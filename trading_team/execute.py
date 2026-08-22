# -*- coding: utf-8 -*-
"""LLM 提案执行器：将老板审批后的交易计划写入 paper/paper.db

用法:
    python trading_team/execute.py            # 执行最新已审批的决策
    python trading_team/execute.py 2026-08-22 # 执行指定日期的决策

规则:
    - 只执行 pm_status == "approved" 的决策
    - 幂等：同一日期的 LLM 交易已写入则跳过
    - 收盘价成交、免手续费、无 T+1（与 v1 paper 一致）
    - 通过 trades.reason 标注 "LLM提案" 以区分 v1 策略交易
"""
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DECISIONS_DIR = ROOT / "trading_team" / "decisions"
DB = ROOT / "paper" / "paper.db"
INIT_CASH = 100_000.0


def _db():
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS account(day TEXT PRIMARY KEY, cash REAL, equity REAL, note TEXT);
    CREATE TABLE IF NOT EXISTS positions(code TEXT PRIMARY KEY, qty REAL, avg_cost REAL);
    CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT, code TEXT, side TEXT, qty REAL, price REAL, amount REAL, reason TEXT);
    """)
    if conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0:
        conn.execute("INSERT INTO account VALUES ('init', ?, ?, '初始资金')",
                     (INIT_CASH, INIT_CASH))
        conn.commit()
    return conn


def load_decision(day):
    path = DECISIONS_DIR / day / "decision.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def latest_bundle_day():
    """从 bundle 数据获取最新交易日（简化：读 decision.json 里的日期）"""
    return None  # 由调用方传入


def load_close_price(code, day):
    """从 bundle 加载指定股票指定日期的收盘价。简化：尝试 kde_levels bundle。"""
    sys.path.insert(0, str(ROOT / "kde_levels"))
    try:
        from levels import BundleData  # noqa: E402
        bd = BundleData()
        # 尝试加后缀
        for suffix in [".XSHG", ".XSHE", ""]:
            try:
                df = bd.load(f"{code}{suffix}")
                if day in df.index.strftime("%Y-%m-%d").values:
                    return float(df.loc[df.index.strftime("%Y-%m-%d") == day, "close"].iloc[0])
                # 回退到最新可用价格
                return float(df["close"].iloc[-1])
            except Exception:
                continue
    except Exception:
        pass
    return None


def execute(day=None):
    if day is None:
        # 自动找最新有 decision 的日期
        dates = sorted([d.name for d in DECISIONS_DIR.iterdir() if d.is_dir()], reverse=True)
        if not dates:
            print("无决策文件")
            return False
        day = dates[0]

    print(f"[{day}] 加载决策...")
    dec = load_decision(day)
    if dec is None:
        print(f"  决策文件不存在: {DECISIONS_DIR / day / 'decision.json'}")
        return False

    if dec.get("pm_status") != "approved":
        print(f"  状态={dec.get('pm_status')}，未 approved，跳过")
        return False

    conn = _db()

    # 幂等检查：该日期是否已有 LLM 交易记录
    if conn.execute(
        "SELECT 1 FROM trades WHERE day=? AND reason LIKE 'LLM提案%' LIMIT 1", (day,)
    ).fetchone():
        print(f"  {day} 已有 LLM 交易记录，幂等跳过")
        conn.close()
        return True

    # 读取当前账户状态
    last = conn.execute(
        "SELECT cash, equity FROM account ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    cash = last[0]
    positions = {
        r[0]: {"qty": r[1], "cost": r[2]}
        for r in conn.execute("SELECT code, qty, avg_cost FROM positions")
    }

    plans = dec.get("plans", [])
    if not plans:
        print("  无交易计划")
        conn.close()
        return True

    trades = []
    executed_codes = set()

    for plan in plans:
        code = plan.get("code", "")
        name = plan.get("name", "")
        direction = plan.get("direction", "")
        pos_pct = plan.get("position_pct", 0) or 0
        entry = plan.get("entry")
        stop = plan.get("stop")
        confidence = plan.get("confidence", 0)

        if not code:
            continue

        # 解析方向
        dir_lower = direction.lower()
        is_buy = "买入" in direction or (pos_pct > 0 and "观望" not in direction and "回避" not in direction)
        is_sell = "卖出" in direction or "减持" in direction or "回避" in direction
        is_hold = "持有" in direction

        price = load_close_price(code, day)
        if price is None:
            print(f"  ⚠️ {code} 无法获取收盘价，跳过")
            continue

        # 当前持仓
        cur_qty = positions.get(code, {}).get("qty", 0)
        cur_val = cur_qty * price

        if is_sell and cur_qty > 0:
            # 卖出全部持仓
            qty = cur_qty
            amt = qty * price
            cash += amt
            trades.append((day, code, "sell", qty, price, amt,
                          f"LLM提案: {name} {direction} 信心{confidence}"))
            del positions[code]
            executed_codes.add(code)
            print(f"  卖出 {code} ×{qty} @ {price:.2f} = ¥{amt:,.0f}")

        elif is_buy and pos_pct > 0:
            # 计算目标市值（基于当前总权益）
            equity_now = cash + sum(p["qty"] * price for c, p in positions.items() if c != code)
            # 用当日实际价格重新估算 equity
            # 简化：先估算所有持仓市值
            total_val = cash
            for c, p in positions.items():
                p_price = price if c == code else load_close_price(c, day)
                if p_price:
                    total_val += p["qty"] * p_price

            target_val = total_val * pos_pct
            target_qty = int(target_val / price / 100) * 100

            if target_qty <= 0:
                print(f"  {code} 目标仓位计算为 0，跳过")
                continue

            if cur_qty > 0:
                # 已有持仓，计算差额
                diff_qty = target_qty - cur_qty
                if diff_qty > 0:
                    buy_qty = diff_qty
                    buy_amt = buy_qty * price
                    if buy_amt > cash:
                        print(f"  ⚠️ {code} 现金不足: 需 ¥{buy_amt:,.0f}, 有 ¥{cash:,.0f}, 按可用现金调整")
                        buy_qty = int(cash / price / 100) * 100
                        if buy_qty <= 0:
                            continue
                        buy_amt = buy_qty * price
                    cash -= buy_amt
                    old = positions[code]
                    new_qty = old["qty"] + buy_qty
                    positions[code] = {
                        "qty": new_qty,
                        "cost": (old["qty"] * old["cost"] + buy_qty * price) / new_qty
                    }
                    trades.append((day, code, "buy", buy_qty, price, buy_amt,
                                  f"LLM提案: {name} 加仓至{pos_pct*100:.0f}% 信心{confidence}"))
                    executed_codes.add(code)
                    print(f"  买入 {code} ×{buy_qty} @ {price:.2f} = ¥{buy_amt:,.0f} (加仓)")
                elif diff_qty < 0:
                    sell_qty = min(abs(diff_qty), cur_qty)
                    sell_amt = sell_qty * price
                    cash += sell_amt
                    positions[code]["qty"] -= sell_qty
                    if positions[code]["qty"] == 0:
                        del positions[code]
                    trades.append((day, code, "sell", sell_qty, price, sell_amt,
                                  f"LLM提案: {name} 减仓至{pos_pct*100:.0f}% 信心{confidence}"))
                    executed_codes.add(code)
                    print(f"  卖出 {code} ×{sell_qty} @ {price:.2f} = ¥{sell_amt:,.0f} (减仓)")
                else:
                    print(f"  {code} 仓位已达标，不动")
            else:
                # 新开仓
                buy_qty = target_qty
                buy_amt = buy_qty * price
                if buy_amt > cash:
                    print(f"  ⚠️ {code} 现金不足: 需 ¥{buy_amt:,.0f}, 有 ¥{cash:,.0f}, 按可用现金调整")
                    buy_qty = int(cash / price / 100) * 100
                    if buy_qty <= 0:
                        continue
                    buy_amt = buy_qty * price
                cash -= buy_amt
                positions[code] = {"qty": buy_qty, "cost": price}
                trades.append((day, code, "buy", buy_qty, price, buy_amt,
                              f"LLM提案: {name} 开仓{pos_pct*100:.0f}% 信心{confidence}"))
                executed_codes.add(code)
                print(f"  买入 {code} ×{buy_qty} @ {price:.2f} = ¥{buy_amt:,.0f} (开仓)")

        elif is_hold and cur_qty > 0:
            print(f"  {code} 持有不动")
            executed_codes.add(code)

        else:
            print(f"  {code} 方向={direction}, 不操作")

    # 写入数据库
    for t in trades:
        conn.execute(
            "INSERT INTO trades(day,code,side,qty,price,amount,reason) VALUES (?,?,?,?,?,?,?)",
            t
        )

    # 重写 positions 表
    conn.execute("DELETE FROM positions")
    conn.executemany(
        "INSERT INTO positions VALUES (?,?,?)",
        [(c, p["qty"], p["cost"]) for c, p in positions.items()]
    )

    # 计算权益
    total_equity = cash
    for c, p in positions.items():
        p_price = load_close_price(c, day)
        if p_price:
            total_equity += p["qty"] * p_price

    note = f"LLM提案执行 {len(trades)} 笔" if trades else "LLM提案: 无交易"
    conn.execute("INSERT OR REPLACE INTO account VALUES (?,?,?,?)", (day, round(cash, 2), round(total_equity, 2), note))
    conn.commit()
    conn.close()

    print(f"\n✅ {day} 执行完成: cash=¥{cash:,.2f}, equity=¥{total_equity:,.2f}, trades={len(trades)}")

    # ── 写入 team.db 执行日志 ───────────────────────────────
    TEAM_DB = ROOT / "trading_team" / "team.db"
    if TEAM_DB.exists():
        tconn = sqlite3.connect(TEAM_DB)
        for t in trades:
            # t = (day, code, side, qty, price, amount, reason)
            tconn.execute("""
                INSERT INTO execution_log
                (decision_date, code, side, qty, price, amount, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, t)
        executed_at = datetime.now().isoformat()
        result_summary = f"执行{len(trades)}笔,cash={cash:.2f},equity={total_equity:.2f}"
        tconn.execute("""
            UPDATE decisions
            SET executed_at = ?, execution_result = ?
            WHERE date = ?
        """, (executed_at, result_summary, day))
        tconn.commit()
        tconn.close()
        print(f"  [team.db] 已记录执行日志 {len(trades)} 条")

    return True


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else None
    execute(day)
