# -*- coding: utf-8 -*-
"""阶段4·交易员：Paper 账户（SQLite 台账，收盘价模拟成交）

语义：以 bundle 最新交易日为"今日"。每月首个交易日按 live 策略调仓，
其余日子只做市值重估（mark-to-market）。T+1 与费用在 v1 简化，复盘中注明。
"""
import sqlite3
import sys
from datetime import date
from pathlib import Path

from .common import ROOT, load_status, now

sys.path.insert(0, str(ROOT / "kde_levels"))
from levels import BundleData  # noqa: E402

DB = ROOT / "paper" / "paper.db"
INIT_CASH = 100000.0

# live 策略信号参数（与 strategies/momentum_rotation.py 对齐）
UNIVERSE = ["600519.XSHG", "000858.XSHE", "600036.XSHG", "601318.XSHG",
            "300750.XSHE", "002594.XSHE", "601899.XSHG", "000333.XSHE",
            "600900.XSHG", "601012.XSHG"]
MOM_DAYS, HOLD_NUM, MA_DAYS = 20, 3, 120
BENCH = "000300.XSHG"


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


def _signals(bd):
    """返回 (target_codes, market_ok, mom_table)"""
    idx = bd.load_index(BENCH)
    idx_close = idx["close"].values
    market_ok = bool(idx_close[-1] > idx_close[-MA_DAYS:].mean())
    table = []
    for s in UNIVERSE:
        df = bd.load(s)
        c = df["close"].values
        if len(c) < MOM_DAYS + 1:
            continue
        table.append((s, c[-1] / c[-MOM_DAYS - 1] - 1, float(c[-1])))
    table.sort(key=lambda x: -x[1])
    targets = [s for s, m, _ in table[:HOLD_NUM]] if market_ok else []
    return targets, market_ok, table


def main():
    st = load_status()
    reg = st.get("strategy_registry", {})
    live = [k for k, v in reg.items() if v["state"] == "live"]
    if not live:
        return {"note": "没有 live 策略，paper 跳过", "live": []}

    bd = BundleData()
    latest = bd.load(UNIVERSE[0]).index[-1].date().isoformat()
    conn = _db()

    if conn.execute("SELECT 1 FROM account WHERE day=?", (latest,)).fetchone():
        equity = conn.execute("SELECT equity FROM account WHERE day=?", (latest,)).fetchone()[0]
        conn.close()
        return {"note": f"{latest} 已记账，幂等跳过", "equity": equity, "live": live}

    last = conn.execute("SELECT cash, equity FROM account ORDER BY rowid DESC LIMIT 1").fetchone()
    cash = last[0]
    positions = {r[0]: {"qty": r[1], "cost": r[2]}
                 for r in conn.execute("SELECT code, qty, avg_cost FROM positions")}

    targets, market_ok, mom = _signals(bd)
    prices = {s: float(bd.load(s)["close"].iloc[-1]) for s in UNIVERSE}
    trades = []

    # 是否调仓日：最新交易日是本月的第一个 bundle 交易日
    month_days = [d for d in bd.load_index(BENCH).index.date if (d.year, d.month) == (date.fromisoformat(latest).year, date.fromisoformat(latest).month)]
    is_rebalance = (not positions and not conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]) \
        or latest == min(month_days).isoformat()

    if is_rebalance:
        # 卖出不在目标内的持仓
        for code in list(positions):
            if code not in targets:
                qty = positions[code]["qty"]
                amt = qty * prices[code]
                cash += amt
                trades.append((latest, code, "sell", qty, prices[code], amt, "调出名单/风控"))
                del positions[code]
        # 等权买入目标
        if targets:
            equity_now = cash + sum(p["qty"] * prices[c] for c, p in positions.items())
            weight = 0.98 / len(targets)
            for code in targets:
                tgt_val = equity_now * weight
                cur_val = positions.get(code, {}).get("qty", 0) * prices[code]
                diff = tgt_val - cur_val
                if abs(diff) > equity_now * 0.02:  # 偏离超 2% 才动
                    qty = int(abs(diff) / prices[code] / 100) * 100  # 整手
                    if qty <= 0:
                        continue
                    if diff > 0 and qty * prices[code] <= cash:
                        cash -= qty * prices[code]
                        old = positions.get(code, {"qty": 0, "cost": 0})
                        new_qty = old["qty"] + qty
                        positions[code] = {"qty": new_qty,
                                           "cost": (old["qty"] * old["cost"] + qty * prices[code]) / new_qty}
                        trades.append((latest, code, "buy", qty, prices[code], qty * prices[code], "动量入选"))
                    elif diff < 0 and code in positions:
                        qty = min(qty, positions[code]["qty"])
                        cash += qty * prices[code]
                        positions[code]["qty"] -= qty
                        trades.append((latest, code, "sell", qty, prices[code], qty * prices[code], "再平衡"))
                        if positions[code]["qty"] == 0:
                            del positions[code]

    equity = cash + sum(p["qty"] * prices[c] for c, p in positions.items())
    for t in trades:
        conn.execute("INSERT INTO trades(day,code,side,qty,price,amount,reason) VALUES (?,?,?,?,?,?,?)", t)
    conn.execute("DELETE FROM positions")
    conn.executemany("INSERT INTO positions VALUES (?,?,?)",
                     [(c, p["qty"], p["cost"]) for c, p in positions.items()])
    note = "调仓日" if is_rebalance else "市值重估"
    conn.execute("INSERT INTO account VALUES (?,?,?,?)", (latest, cash, equity, note))
    conn.commit()

    hist = conn.execute("SELECT day, equity FROM account WHERE day != 'init' ORDER BY day").fetchall()
    peak = max([INIT_CASH] + [e for _, e in hist])
    conn.close()

    return {
        "day": latest, "note": note, "live_strategies": live,
        "market_ok_120ma": market_ok,
        "trades": [{"code": t[1], "side": t[2], "qty": t[3], "price": t[4], "reason": t[6]} for t in trades],
        "cash": round(cash, 2), "equity": round(equity, 2),
        "cum_return": round(equity / INIT_CASH - 1, 4),
        "max_drawdown": round(equity / peak - 1, 4),
        "positions": {c: round(p["qty"] * prices[c], 0) for c, p in positions.items()},
        "momentum_top5": [{"code": s, "mom": round(m, 4)} for s, m, _ in mom[:5]],
        "assumption": "收盘价成交、免手续费、无 T+1 限制（v1 简化）",
    }
