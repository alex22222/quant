# -*- coding: utf-8 -*-
"""Execute version-approved LLM proposals through the shared A-share Paper model."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from paper.execution_model import Bar, portfolio_value
from trading_team import consistency_check
from trading_team.counterfactual import load_execution_bars
from trading_team.entry_contract import simulate_entry
from trading_team.exit_contract import simulate_exit
from trading_team.schemas import SchemaError, compute_executable, is_buy, is_sell, parse_pct
from trading_team.state_lock import state_lock
from trading_team.approval_audit import inspect_projection
from trading_team.paper_inventory import attach_inventory, consume_position, inventory_basis, read_trades

ROOT = Path(__file__).parent.parent
DECISIONS_DIR = ROOT / "trading_team" / "decisions"
DB = ROOT / "paper" / "paper.db"
INIT_CASH = 100_000.0
SLIPPAGE_RATE = 0.002


def _ensure_column(conn, table: str, name: str, declaration: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _db():
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS account(day TEXT PRIMARY KEY, cash REAL, equity REAL, note TEXT);
    CREATE TABLE IF NOT EXISTS positions(code TEXT PRIMARY KEY, qty REAL, avg_cost REAL, buy_day TEXT);
    CREATE TABLE IF NOT EXISTS pending_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_day TEXT, code TEXT, side TEXT, qty INTEGER,
        planned_price REAL, signal_source TEXT,
        status TEXT DEFAULT 'pending', reason TEXT);
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT, code TEXT, side TEXT, qty REAL, price REAL, amount REAL,
        fee REAL DEFAULT 0, reason TEXT,
        signal_day TEXT, signal_source TEXT, data_version TEXT,
        planned_price REAL, fill_price REAL, price_type TEXT, reject_reason TEXT);
    """)
    _ensure_column(conn, "pending_orders", "plan_hash", "TEXT")
    _ensure_column(conn, "pending_orders", "execution_day", "TEXT")
    _ensure_column(conn, "pending_orders", "fee", "REAL DEFAULT 0")
    _ensure_column(conn, "pending_orders", "fill_price", "REAL")
    if conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0:
        conn.execute("INSERT INTO account VALUES ('init', ?, ?, '初始资金')",
                     (INIT_CASH, INIT_CASH))
        conn.commit()
    return conn


def load_decision(day: str):
    path = DECISIONS_DIR / day / "decision.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _simulate_buy(plan: dict, qty: int, bar: Bar):
    return simulate_entry(plan, qty, bar, slippage_rate=SLIPPAGE_RATE)


def execute(day: str | None = None, confirm: bool = False) -> bool:
    from pipeline.policy import require
    require("llm_execution")
    with state_lock(DECISIONS_DIR.parent):
        return _execute(day, confirm)


def _execute(day: str | None, confirm: bool) -> bool:
    if day is None:
        dates = sorted(d.name for d in DECISIONS_DIR.iterdir() if d.is_dir())
        if not dates:
            print("无决策文件")
            return False
        day = dates[-1]
    mode = "EXECUTE" if confirm else "DRY-RUN"
    print(f"[{day}] [{mode}] 加载决策...")

    check = consistency_check.check_day(day)
    for conflict in check["conflicts"]:
        print(f"  [冲突] {conflict}")
    for warning in check["warnings"]:
        print(f"  [警告] {warning}")
    if not check["ok"]:
        print("  一致性校验未通过，拒绝执行")
        return False

    decision = load_decision(day)
    if decision is None:
        return False
    try:
        audit = inspect_projection(decision, DECISIONS_DIR.parent)
        if audit["status"] == "pending":
            print("  审批同步未完成，拒绝执行")
            return False
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"  审批事件证据冲突，拒绝执行: {exc}")
        return False
    if decision.get("pm_status") not in ("approved", "partial"):
        print(f"  状态={decision.get('pm_status')}，无已批计划")
        return False
    plan_hash = decision.get("plan_hash")
    if not plan_hash:
        print("  缺少 plan_hash，拒绝执行")
        return False
    from trading_team.multica_reviews import ReviewBlocked, require_reviews
    try:
        require_reviews(day, plan_hash, team=DECISIONS_DIR.parent)
    except ReviewBlocked as exc:
        print(f"  {exc}")
        return False

    actionable = [p for p in decision.get("plans", []) if p.get("executable")]
    if any("protection" in p for p in decision.get("plans", [])):
        print("  跨日保护仅支持独立双账户 Paper 管线，拒绝旧执行入口")
        return False
    codes = [str(p["code"]) for p in actionable]
    conn = _db()
    if confirm and conn.execute(
        "SELECT 1 FROM pending_orders WHERE execution_day=? AND signal_source='llm' LIMIT 1",
        (day,),
    ).fetchone():
        print(f"  {day} 已有 LLM Paper 订单结果，幂等跳过")
        conn.close()
        return True

    cash, _ = conn.execute(
        "SELECT cash, equity FROM account ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    positions = {
        row[0]: {"qty": row[1], "cost": row[2], "buy_day": row[3]}
        for row in conn.execute("SELECT code, qty, avg_cost, buy_day FROM positions")
    }
    codes = sorted(set(codes) | positions.keys())
    try:
        bars = load_execution_bars(day, codes, ROOT / "trading_team") if codes else {}
        marked_equity = portfolio_value(cash, positions, bars, price_field="prev_close")
        attach_inventory(positions, inventory_basis(read_trades(conn), day))
    except Exception:
        conn.close()
        raise
    outcomes, trades = [], []
    buy_cash = cash

    for plan in actionable:
        code = str(plan["code"])
        if plan.get("approved_plan_hash") != plan_hash:
            print(f"  {code} 审批版本失效，跳过")
            continue
        bar = bars.get(code)
        if bar is None:
            outcomes.append((code, "buy", 0, None, "rejected", "无行情", 0, None))
            continue
        try:
            pct = parse_pct(plan.get("position_pct"))
        except SchemaError as exc:
            outcomes.append((code, "buy", 0, None, "rejected", str(exc), 0, None))
            continue
        eligible, reason = compute_executable(plan, bool(decision.get("market_ok")), plan_hash)
        if not eligible:
            side = "sell" if is_sell(str(plan.get("direction", ""))) else "buy"
            outcomes.append((code, side, 0, None, "blocked", reason, 0, None))
            continue
        if plan.get("approval") == "reduced":
            pct /= 2
        direction = str(plan.get("direction", ""))
        current = positions.get(code, {"qty": 0, "cost": 0, "buy_day": None})

        if is_sell(direction):
            fill, planned = simulate_exit(plan, current, bar, day, slippage_rate=SLIPPAGE_RATE)
            outcomes.append((code, "sell", fill.qty, planned, fill.status,
                             fill.reject_reason, fill.fee, fill.price or None))
            if fill.filled:
                cash += fill.amount - fill.fee
                consume_position(current, fill.qty, day)
                if not current["qty"]:
                    positions.pop(code, None)
                trades.append((code, "sell", fill, plan))
            continue

        if not is_buy(direction, pct) or pct <= 0:
            continue
        entry = plan.get("entry") or []
        sizing_price = max(entry) if entry else (bar.open or bar.close)
        target_qty = int(marked_equity * pct / sizing_price / 100) * 100
        qty = max(target_qty - current["qty"], 0)
        fill, planned, pre_reason = _simulate_buy(plan, qty, bar)
        if fill is None:
            status = "unfilled" if "未触发" in pre_reason else "rejected"
            outcomes.append((code, "buy", qty, planned, status, pre_reason, 0, None))
            continue
        if fill.filled and fill.amount + fill.fee > buy_cash:
            affordable = int(buy_cash / (fill.price * 1.001) / 100) * 100
            fill, planned, _ = _simulate_buy(plan, affordable, bar)
        if not fill.filled or fill.amount + fill.fee > buy_cash:
            reason = fill.reject_reason or "现金不足"
            outcomes.append((code, "buy", qty, planned, fill.status, reason,
                             fill.fee, fill.price or None))
            continue
        cash -= fill.amount + fill.fee
        buy_cash -= fill.amount + fill.fee
        new_qty = current["qty"] + fill.qty
        positions[code] = {
            "qty": new_qty,
            "cost": ((current["qty"] * current["cost"] + fill.amount + fill.fee)
                     / new_qty),
            "buy_day": day,
        }
        outcomes.append((code, "buy", fill.qty, planned, "filled", "",
                         fill.fee, fill.price))
        trades.append((code, "buy", fill, plan))

    equity = portfolio_value(cash, positions, bars)
    for code, side, qty, planned, status, reason, fee, fill_price in outcomes:
        print(f"  {code} {side} {status}: qty={qty}, planned={planned}, "
              f"fill={fill_price}, fee={fee:.2f}{' ' + reason if reason else ''}")

    if not confirm:
        conn.close()
        print(f"[DRY-RUN] orders={len(outcomes)}, fills={len(trades)}, equity={equity:,.2f}")
        return True

    for code, side, qty, planned, status, reason, fee, fill_price in outcomes:
        conn.execute(
            "INSERT INTO pending_orders(signal_day,code,side,qty,planned_price,"
            "signal_source,status,reason,plan_hash,fee,fill_price,execution_day) "
            "VALUES (?,?,?,?,?,'llm',?,?,?,?,?,?)",
            (decision.get("data_asof"), code, side, qty, planned, status, reason,
             plan_hash, fee, fill_price, day),
        )
    for code, side, fill, plan in trades:
        conn.execute(
            "INSERT INTO trades(day,code,side,qty,price,amount,fee,reason,signal_day,"
            "signal_source,data_version,planned_price,fill_price,price_type,reject_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,'llm',?,?,?,?,NULL)",
            (day, code, side, fill.qty, fill.price, fill.amount, fill.fee,
             f"LLM提案: {plan.get('name', code)}", decision.get("data_asof"),
             plan_hash, (plan.get("exit") or {}).get("price") if side == "sell" else (plan.get("entry") or [None])[0],
             fill.price, fill.price_type),
        )
    conn.execute("DELETE FROM positions")
    conn.executemany(
        "INSERT INTO positions VALUES (?,?,?,?)",
        [(code, p["qty"], p["cost"], p.get("buy_day")) for code, p in positions.items()],
    )
    conn.execute(
        "INSERT OR REPLACE INTO account VALUES (?,?,?,?)",
        (day, round(cash, 2), round(equity, 2),
         f"LLM Paper: {len(trades)} fills / {len(outcomes)} orders"),
    )
    conn.commit()
    conn.close()

    team_db = ROOT / "trading_team" / "team.db"
    if team_db.exists():
        tconn = sqlite3.connect(team_db)
        tconn.execute(
            "UPDATE decisions SET executed_at=?, execution_result=? WHERE date=?",
            (datetime.now().isoformat(timespec="seconds"),
             f"Paper {len(trades)} fills/{len(outcomes)} orders", day),
        )
        tconn.commit()
        tconn.close()
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("day", nargs="?")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if execute(args.day, args.confirm) else 1)


if __name__ == "__main__":
    main()
