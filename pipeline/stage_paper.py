# -*- coding: utf-8 -*-
"""阶段4·交易员：Paper 账户 v2（SQLite 台账，T+1 开盘模拟成交）

语义（改进计划 Phase 1）：
- 信号从 status.json 注册表加载 live 策略，调用统一接口 generate_targets()，
  本文件不再保存任何策略参数；
- 信号日 T 收盘生成目标 → 写入 pending_orders；T+1（bundle 出现更新的交易日）按开盘价
  撮合，带涨跌停/停牌/整手/T+1/费用约束（paper/execution_model.py）；
- 其余日子只做市值重估（mark-to-market）；
- 每笔交易记录信号日、信号来源、数据版本、计划价、成交价、费用、失败原因；
- 幂等：account 以 day 为主键，pending_orders 以 status 推进，可安全重跑。
"""
import sqlite3
import sys
from datetime import date
from pathlib import Path

from .common import ROOT, load_status, now

sys.path.insert(0, str(ROOT))
from strategies.base import BundleDataAdapter, load_strategy  # noqa: E402
from paper.execution_model import (  # noqa: E402
    DEFAULT_FEES, bar_from_bundle, round_lot, simulate_fill, total_fee,
)

DB = ROOT / "paper" / "paper.db"
INIT_CASH = 100000.0
SCHEMA_VERSION = 2


def _db():
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS account(day TEXT PRIMARY KEY, cash REAL, equity REAL, note TEXT);
    CREATE TABLE IF NOT EXISTS positions(code TEXT PRIMARY KEY, qty REAL, avg_cost REAL, buy_day TEXT);
    CREATE TABLE IF NOT EXISTS pending_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_day TEXT, code TEXT, side TEXT, qty INTEGER,
        planned_price REAL, signal_source TEXT,
        status TEXT DEFAULT 'pending',  -- pending/filled/rejected
        reason TEXT);
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT, code TEXT, side TEXT, qty REAL, price REAL, amount REAL,
        fee REAL DEFAULT 0, reason TEXT,
        signal_day TEXT, signal_source TEXT, data_version TEXT,
        planned_price REAL, fill_price REAL, price_type TEXT, reject_reason TEXT);
    """)
    if conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0:
        conn.execute("INSERT INTO account VALUES ('init', ?, ?, '初始资金')",
                     (INIT_CASH, INIT_CASH))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def _load_live_strategy(st):
    """从注册表找 live 策略。返回 (name, module, params)；无 live 返回 None。"""
    reg = st.get("strategy_registry", {})
    live = [(k, v) for k, v in reg.items() if v.get("state") == "live"]
    if not live:
        return None
    if len(live) > 1:
        names = [k for k, _ in live]
        raise RuntimeError(f"存在多个 live 策略 {names}，注册表必须先收敛到一个")
    name, entry = live[0]
    mod = load_strategy(name)
    params = dict(getattr(mod, "PARAMS", {}))
    params.update(entry.get("params") or {})
    return name, mod, params


def _last_close(bd, code):
    try:
        return float(bd.load(code)["close"].iloc[-1])
    except Exception:
        return None


def _equity(conn, bd, cash, positions):
    eq = cash
    for c, p in positions.items():
        px = _last_close(bd, c)
        if px:
            eq += p["qty"] * px
    return eq


def _fill_pending(conn, bd, latest, cash, positions, trades):
    """撮合所有 signal_day < latest 的挂单。卖出优先（释放现金）。返回新的 cash。"""
    rows = conn.execute("""
        SELECT id, signal_day, code, side, qty, planned_price, signal_source
        FROM pending_orders WHERE status='pending' AND signal_day < ?
        ORDER BY CASE side WHEN 'sell' THEN 0 ELSE 1 END, id
    """, (latest,)).fetchall()
    index_latest = bd.load_index("000300.XSHG").index[-1].date().isoformat()

    for oid, sday, code, side, qty, planned, source in rows:
        bar = bar_from_bundle(bd, code, latest, index_latest)

        # T+1：买入当日不可卖
        pos = positions.get(code)
        if side == "sell" and pos and pos.get("buy_day") == latest:
            conn.execute("UPDATE pending_orders SET status='rejected', reason=? WHERE id=?",
                         ("T+1 限制：买入当日不可卖", oid))
            trades.append((latest, code, "sell", 0, 0, 0, 0, "T+1 限制",
                           sday, source, latest, planned, None, None, "T+1 限制"))
            continue

        if side == "sell" and (not pos or pos["qty"] <= 0):
            conn.execute("UPDATE pending_orders SET status='rejected', reason=? WHERE id=?",
                         ("无持仓可卖", oid))
            continue
        if side == "sell":
            qty = min(qty, int(pos["qty"]))

        if side == "buy":
            # 现金约束：含费用预估，不够则向下整手
            while qty > 0:
                amt = qty * (bar.open or bar.close or 0)
                if amt and amt + total_fee("buy", amt, DEFAULT_FEES) <= cash:
                    break
                qty -= 100
            if qty <= 0:
                conn.execute("UPDATE pending_orders SET status='rejected', reason=? WHERE id=?",
                             ("现金不足", oid))
                trades.append((latest, code, "buy", 0, 0, 0, 0, "现金不足",
                               sday, source, latest, planned, None, None, "现金不足"))
                continue

        fill = simulate_fill(side, qty, bar, DEFAULT_FEES)
        if not fill.filled:
            conn.execute("UPDATE pending_orders SET status='rejected', reason=? WHERE id=?",
                         (fill.reject_reason, oid))
            trades.append((latest, code, side, 0, 0, 0, 0, fill.reject_reason,
                           sday, source, latest, planned, None, None, fill.reject_reason))
            continue

        if side == "buy":
            cash -= fill.amount + fill.fee
            old = positions.get(code, {"qty": 0, "cost": 0})
            new_qty = old["qty"] + fill.qty
            positions[code] = {
                "qty": new_qty,
                "cost": (old["qty"] * old["cost"] + fill.qty * fill.price) / new_qty,
                "buy_day": latest,
            }
        else:
            cash += fill.amount - fill.fee
            pos["qty"] -= fill.qty
            if pos["qty"] <= 0:
                del positions[code]

        conn.execute("UPDATE pending_orders SET status='filled', reason=? WHERE id=?",
                     (f"成交 {fill.qty}@{fill.price} ({fill.price_type}), 费 {fill.fee}", oid))
        trades.append((latest, code, side, fill.qty, fill.price, fill.amount, fill.fee,
                       "调仓成交", sday, source, latest, planned, fill.price,
                       fill.price_type, None))
    return cash


def _make_rebalance_orders(conn, targets, latest, cash, positions, source, universe, bd,
                           weight_cap=0.98):
    """按目标名单生成挂单（等权 weight_cap）。返回新挂单数。"""
    prices = {s: _last_close(bd, s) for s in set(universe) | set(positions)}
    created = 0
    # 卖出：不在目标内的全部持仓
    for code in list(positions):
        if code not in targets:
            qty = int(positions[code]["qty"])
            if qty > 0:
                conn.execute("""
                    INSERT INTO pending_orders(signal_day, code, side, qty, planned_price, signal_source)
                    VALUES (?,?,?,?,?,?)
                """, (latest, code, "sell", qty, prices.get(code), source))
                created += 1
    # 买入/调整：等权目标
    if targets:
        equity_now = cash + sum(
            p["qty"] * (prices.get(c) or 0) for c, p in positions.items())
        weight = weight_cap / len(targets)
        for code in targets:
            px = prices.get(code)
            if not px:
                continue
            tgt_val = equity_now * weight
            cur_val = positions.get(code, {}).get("qty", 0) * px
            diff = tgt_val - cur_val
            if abs(diff) <= equity_now * 0.02:  # 偏离 2% 以内不动
                continue
            qty = round_lot(abs(diff) / px)
            if qty <= 0:
                continue
            side = "buy" if diff > 0 else "sell"
            conn.execute("""
                INSERT INTO pending_orders(signal_day, code, side, qty, planned_price, signal_source)
                VALUES (?,?,?,?,?,?)
            """, (latest, code, side, qty, px, source))
            created += 1
    return created


def main():
    st = load_status()
    live = _load_live_strategy(st)
    if live is None:
        return {"note": "没有 live 策略，paper 跳过", "live": []}
    strat_name, strat_mod, params = live

    data = BundleDataAdapter()
    bd = data._bd
    latest = data.latest()
    conn = _db()

    pending_open = conn.execute(
        "SELECT COUNT(*) FROM pending_orders WHERE status='pending' AND signal_day < ?",
        (latest,)).fetchone()[0]
    accounted = conn.execute("SELECT 1 FROM account WHERE day=?", (latest,)).fetchone()
    if accounted and pending_open == 0:
        equity = conn.execute("SELECT equity FROM account WHERE day=?", (latest,)).fetchone()[0]
        conn.close()
        return {"note": f"{latest} 已记账，幂等跳过", "equity": equity,
                "live": [strat_name]}

    last = conn.execute(
        "SELECT cash, equity FROM account ORDER BY rowid DESC LIMIT 1").fetchone()
    cash = last[0]
    positions = {r[0]: {"qty": r[1], "cost": r[2], "buy_day": r[3]}
                 for r in conn.execute("SELECT code, qty, avg_cost, buy_day FROM positions")}
    trades = []

    # 1) 撮合此前的挂单（T+1 开盘）
    cash = _fill_pending(conn, bd, latest, cash, positions, trades)

    # 2) 信号日判定：月线策略=每月 bundle 第一个交易日；日线策略=每个交易日
    idx = bd.load_index(params["benchmark"])
    month_days = [d for d in idx.index.date
                  if (d.year, d.month) == (date.fromisoformat(latest).year,
                                           date.fromisoformat(latest).month)]
    freq = getattr(strat_mod, "FREQUENCY", "monthly")
    is_rebalance = (not positions
                    and conn.execute("SELECT COUNT(*) FROM trades WHERE qty > 0").fetchone()[0] == 0) \
        or latest == min(month_days).isoformat() \
        or freq == "daily"

    signal = None
    n_orders = 0
    if is_rebalance and not conn.execute(
            "SELECT 1 FROM pending_orders WHERE signal_day=?", (latest,)).fetchone():
        signal = strat_mod.generate_targets(data, params, positions=positions)
        n_orders = _make_rebalance_orders(
            conn, signal.targets, latest, cash, positions,
            strat_name, params["universe"], bd,
            weight_cap=params.get("weight_cap", 0.98))
        if not signal.market_ok and positions:
            # 风控：清仓挂单（_make_rebalance_orders 已按空 targets 卖出全部）
            pass

    equity = _equity(conn, bd, cash, positions)

    for t in trades:
        conn.execute("""
            INSERT INTO trades(day, code, side, qty, price, amount, fee, reason,
                               signal_day, signal_source, data_version,
                               planned_price, fill_price, price_type, reject_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, t)
    conn.execute("DELETE FROM positions")
    conn.executemany("INSERT INTO positions VALUES (?,?,?,?)",
                     [(c, p["qty"], p["cost"], p.get("buy_day"))
                      for c, p in positions.items()])
    note = ("调仓信号日" if is_rebalance else "市值重估") + \
           (f"·撮合{len([t for t in trades if t[3] > 0])}笔" if trades else "")
    conn.execute("INSERT OR REPLACE INTO account VALUES (?,?,?,?)",
                 (latest, round(cash, 2), round(equity, 2), note))
    conn.commit()

    hist = conn.execute(
        "SELECT day, equity FROM account WHERE day != 'init' ORDER BY day").fetchall()
    peak = max([INIT_CASH] + [e for _, e in hist])
    pend = conn.execute(
        "SELECT code, side, qty, planned_price FROM pending_orders WHERE status='pending'"
    ).fetchall()
    conn.close()

    return {
        "day": latest, "note": note, "live_strategies": [strat_name],
        "strategy_family": getattr(strat_mod, "FAMILY", strat_name),
        "market_ok": (signal.market_ok if signal else None),
        "new_orders": n_orders,
        "pending_orders": [
            {"code": c, "side": s, "qty": q, "planned_price": p} for c, s, q, p in pend
        ],
        "fills": [{"code": t[1], "side": t[2], "qty": t[3], "price": t[4],
                   "fee": t[6], "reason": t[7]} for t in trades],
        "cash": round(cash, 2), "equity": round(equity, 2),
        "cum_return": round(equity / INIT_CASH - 1, 4),
        "max_drawdown": round(equity / peak - 1, 4),
        "positions": {c: round(p["qty"] * (_last_close(bd, c) or 0), 0)
                      for c, p in positions.items()},
        "momentum_top5": (signal.detail.get("momentum_table", [])[:5] if signal else []),
        "assumption": ("信号日T收盘出信号，T+1开盘成交；涨跌停/停牌不可成交；"
                       "整手；T+1卖出限制；佣金万2.5(最低5元)+印花税万5(卖出)+过户费万0.2"),
    }
