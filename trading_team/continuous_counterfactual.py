"""Continuous, non-executing counterfactuals using isolated in-memory Paper accounts."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import date, datetime, time
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from trading_team.collect import DEFAULT_TRADING_CALENDAR, load_trading_days
from trading_team.execution_bars import SHANGHAI, TEAM, _bar_from_row, validate_document
from trading_team.paper_pipeline import (
    PAPER_AUTO_START, _db, _execute_portfolio_transaction, _execution_inputs, build_paper_decision,
)
from trading_team.paper_plans import select_paper_plans
from trading_team.risk_contract import validate_risk_document
from trading_team.state_lock import atomic_write_json, atomic_write_text, state_lock
from trading_team.paper_inventory import attach_inventory, available_quantity, inventory_basis, read_trades
from trading_team.paper_protection import read_protection

SCHEMA = "continuous_counterfactual.v1"
READ_ERRORS = (OSError, ValueError, TypeError, KeyError, sqlite3.Error)


def _hash(value) -> str:
    content = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(content).hexdigest()


def _read(path: Path) -> bytes | None:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("counterfactual source symlink is forbidden")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def runtime_fingerprints() -> dict:
    names = ("paper/execution_model.py", "trading_team/paper_pipeline.py", "trading_team/execute.py",
             "trading_team/continuous_counterfactual.py", "trading_team/paper_plans.py", "trading_team/schemas.py",
             "trading_team/entry_contract.py", "trading_team/exit_contract.py", "trading_team/risk_contract.py",
             "trading_team/order_conditions.py", "trading_team/execution_bars.py", "trading_team/paper_inventory.py",
             "trading_team/protection_contract.py", "trading_team/paper_protection.py")
    return {name: _hash((TEAM.parent / name).read_bytes()) for name in names}


def _positions(conn: sqlite3.Connection) -> dict:
    return {row[0]: {"qty": row[1], "avg_cost": row[2], "buy_day": row[3]}
            for row in conn.execute("SELECT code,qty,avg_cost,buy_day FROM positions ORDER BY code")}


def _account(conn: sqlite3.Connection, day: str, previous_equity: float, initial_cash: float) -> dict:
    cash, equity = conn.execute("SELECT cash,equity FROM account WHERE day=?", (day,)).fetchone()
    fields = ("code", "side", "qty", "status", "reason", "fill_price", "fee", "plan_hash")
    orders = [dict(zip(fields, row)) for row in conn.execute(
        "SELECT code,side,qty,status,reason,fill_price,fee,plan_hash FROM pending_orders WHERE execution_day=? ORDER BY id", (day,))]
    positions = _positions(conn)
    inventory = inventory_basis(read_trades(conn), day)
    attach_inventory(positions, inventory)
    for position in positions.values():
        position["available_qty"] = available_quantity(position, day)
        position["frozen_qty"] = position["qty"] - position["available_qty"]
    history = read_protection(conn)
    protection = {"active": history["active"], "events": history["days"].get(day, {}).get("events", [])}
    return {"cash": cash, "equity": equity, "positions": positions, "inventory": inventory, "protection": protection, "orders": orders,
            "daily_return": round(equity / previous_equity - 1, 8),
            "cumulative_return": round(equity / initial_cash - 1, 8),
            "fees": round(sum(row["fee"] for row in orders if row["status"] == "filled"), 2)}


def build_series(start: str, end: str, *, team: Path = TEAM, calendar_path: Path = DEFAULT_TRADING_CALENDAR,
                 now: datetime | None = None, initial_cash: float = 100000) -> dict:
    if any(date.fromisoformat(day).isoformat() != day for day in (start, end)) or start > end:
        raise ValueError("invalid counterfactual date range")
    if isinstance(initial_cash, bool) or not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("initial cash must be finite and positive")
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    calendar_bytes = calendar_path.read_bytes()
    calendar = sorted(load_trading_days(calendar_path))
    if not calendar or start < calendar[0] or end > calendar[-1]:
        raise ValueError("calendar does not cover requested range")
    sessions = [day for day in calendar if start <= day <= end]
    if not sessions:
        raise ValueError("requested range contains no trading sessions")
    source = {}
    for day in sessions:
        for relative in (f"outputs/{day}/plan.json", f"replay_data/{day}.json"):
            source[relative] = _read(team / relative)
    inputs = {"files": {key: _hash(raw) if raw is not None else None for key, raw in source.items()},
              "calendar_hash": _hash(calendar_bytes), "policy": _execution_inputs({}),
              "runtime_fingerprints": runtime_fingerprints(),
              "completed_sessions": [day for day in sessions if day < current.date().isoformat()
                                     or day == current.date().isoformat() and current.time() >= time(15, 10)],
              "schema": SCHEMA, "start": start, "end": end, "initial_cash": initial_cash}
    rows = []
    previous_equity = {name: initial_cash for name in ("gate", "no_gate", "csi300")}
    previous_bars = None
    benchmark_base = None
    gap = False
    with ExitStack() as stack:
        accounts = {name: stack.enter_context(closing(_db(None))) for name in ("gate", "no_gate")}
        for conn in accounts.values():
            conn.execute("UPDATE account SET cash=?,equity=? WHERE day='init'", (initial_cash, initial_cash))
            conn.commit()
        for day in sessions:
            row = {"date": day, "status": "not_evaluated", "portfolios": None}
            rows.append(row)
            if gap:
                row["reason"] = "prior session gap; no reset or forward fill"
                continue
            try:
                if day > current.date().isoformat() or day == current.date().isoformat() and current.time() < time(15, 10):
                    raise ValueError("execution session not completed")
                raw = source[f"outputs/{day}/plan.json"]
                if raw is None:
                    raise ValueError("plan missing; absence is not a verified hold instruction")
                plan = json.loads(raw)
                if plan.get("date") != day:
                    raise ValueError("plan date mismatch")
                signal_day = plan.get("data_asof")
                if signal_day not in calendar or signal_day >= day:
                    raise ValueError("plan data date missing, invalid or retrospective")
                if not validate_risk_document(plan):
                    raise ValueError("structured risk evidence missing; legacy proposals not recertified")
                raw_market = source[f"replay_data/{day}.json"]
                if raw_market is None:
                    raise ValueError("validated execution cache missing; no network or repair in counterfactual")
                market = validate_document(day, json.loads(raw_market))
                bars = {code: _bar_from_row(code, value, day) for code, value in market["bars"].items()}
                holdings = set().union(*(_positions(conn).keys() for conn in accounts.values()))
                needed = holdings | {"000300"} | {p["code"] for p in select_paper_plans(plan)}
                if needed - bars.keys():
                    raise ValueError("required execution/holding prices missing: " + ",".join(sorted(needed - bars.keys())))
                if previous_bars is not None:
                    for code in holdings | {"000300"}:
                        if abs(bars[code].prev_close - previous_bars[code].close) > 1e-6:
                            raise ValueError(f"{code}: price continuity gap or unmodeled corporate action")
                decision = build_paper_decision(plan)
                values = {}
                for name, conn in accounts.items():
                    _execute_portfolio_transaction(decision, name, conn, bars, True)
                    values[name] = _account(conn, day, previous_equity[name], initial_cash)
                benchmark = bars["000300"]
                benchmark_base = benchmark_base or benchmark.prev_close
                equity = round(initial_cash * benchmark.close / benchmark_base, 2)
                values["csi300"] = {"equity": equity,
                    "daily_return": round(equity / previous_equity["csi300"] - 1, 8),
                    "cumulative_return": round(equity / initial_cash - 1, 8)}
                previous_equity = {name: value["equity"] for name, value in values.items()}
                previous_bars = bars
                row.update(status="evaluated", portfolios=values, plan_hash=decision["plan_hash"],
                           market_data_hash=market["payload_hash"], data_asof=signal_day)
            except (AttributeError, *READ_ERRORS) as exc:
                gap = True
                row.update(status="gap", reason=str(exc))
    if (calendar_path.read_bytes() != calendar_bytes or runtime_fingerprints() != inputs["runtime_fingerprints"]
            or any(_read(team / key) != raw for key, raw in source.items())):
        raise ValueError("counterfactual inputs changed while evaluating; retry from fresh evidence")
    evaluated = [row for row in rows if row["status"] == "evaluated"]
    summary = None
    if not gap:
        summary = {}
        for name in ("gate", "no_gate", "csi300"):
            peak = initial_cash
            drawdown = 0
            for row in evaluated:
                equity = row["portfolios"][name]["equity"]
                peak = max(peak, equity)
                drawdown = min(drawdown, equity / peak - 1)
            last = evaluated[-1]["portfolios"][name]
            summary[name] = {"equity": last["equity"], "return": last["cumulative_return"],
                             "max_drawdown": round(drawdown, 8)}
    report = {"schema_version": SCHEMA, "status": "incomplete" if gap else "complete",
              "evidence_kind": "CONTINUOUS_DAILY_BAR_COUNTERFACTUAL", "forward_performance": "unverified",
              "production_account_used": False, "execution_authorized": False,
              "start": start, "end": end, "initial_cash": initial_cash,
              "expected_sessions": len(sessions), "covered_sessions": len(evaluated),
              "verified_through": evaluated[-1]["date"] if evaluated else None,
              "inputs": inputs, "input_hash": _hash(inputs), "rows": rows, "summary": summary,
              "limitations": ["Retrospective model experiment, not actual Paper fills or verified forward publication",
                              "Cash and holdings carried across every calendar session; stop at first evidence gap",
                              "CSI300 price index only, not an executable ETF or total-return benchmark",
                              "No corporate-action accounting; detected reference-price discontinuities stop the series",
                              "Existing Paper policies: daily bars, DAY exits, no intraday reuse of sale proceeds",
                              "FIFO acquisition quantities from trade ledger; weighted-average costs unchanged",
                              "Explicit versioned standing stops for existing positions; no buy-attached protection",
                              "No model of intraday queue liquidity"]}
    return {**report, "report_hash": _hash(report)}


def render_markdown(report: dict) -> str:
    lines = [f"# {report['start']} 至 {report['end']} 连续账户反事实", "",
             "> 连续现金与持仓的日线模型实验，不是官方 Paper 成交或前向业绩。", "",
             f"覆盖 {report['covered_sessions']}/{report['expected_sessions']} 个交易日；状态 {report['status']}。",
             f"输入版本 `{report['input_hash']}`；报告哈希 `{report['report_hash']}`。", "",
             "| 日期 | 闸门权益 | 无闸门权益 | 沪深300价格指数等值 | 状态 |", "|---|---:|---:|---:|---|"]
    for row in report["rows"]:
        values = row["portfolios"]
        figures = [f"{values[name]['equity']:,.2f}" if values else "未知" for name in ("gate", "no_gate", "csi300")]
        lines.append(f"| {row['date']} | {' | '.join(figures)} | {row.get('reason', row['status']).replace('|', '/')} |")
    lines += ["", "## 边界", ""] + ["- " + value for value in report["limitations"]]
    if report["summary"] is None:
        lines += ["", "覆盖不完整，不输出全区间累计收益；缺口之后不重置账户，也不填充零收益。"]
    return "\n".join(lines) + "\n"


def write_series(end: str, *, start: str = PAPER_AUTO_START, team: Path = TEAM,
                 calendar_path: Path = DEFAULT_TRADING_CALENDAR, now: datetime | None = None) -> dict:
    with state_lock(team):
        report = build_series(start, end, team=team, calendar_path=calendar_path, now=now)
        out = team / "outputs" / end
        atomic_write_json(out / "continuous_counterfactual.json", report)
        atomic_write_text(out / "continuous_counterfactual.md", render_markdown(report))
        return report


def current_series(*, start: str = PAPER_AUTO_START, end: str | None = None, team: Path = TEAM,
                   calendar_path: Path = DEFAULT_TRADING_CALENDAR, now: datetime | None = None) -> dict:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if end is None:
        calendar = sorted(load_trading_days(calendar_path))
        today = current.date().isoformat()
        if not calendar or not calendar[0] <= today <= calendar[-1]:
            raise ValueError("calendar does not cover current date")
        completed = [day for day in calendar if day < today or day == today and current.time() >= time(15, 10)]
        if not completed:
            raise ValueError("no completed session")
        end = completed[-1]
    return build_series(start, end, team=team, calendar_path=calendar_path, now=current)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=PAPER_AUTO_START)
    parser.add_argument("--end", required=True)
    parser.add_argument("--team", type=Path, default=TEAM)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_TRADING_CALENDAR)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        report = (write_series(args.end, start=args.start, team=args.team, calendar_path=args.calendar) if args.write
                  else build_series(args.start, args.end, team=args.team, calendar_path=args.calendar))
    except READ_ERRORS as exc:
        report = {"status": "invalid", "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "complete" else 2)


if __name__ == "__main__":
    main()
