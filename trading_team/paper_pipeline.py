# -*- coding: utf-8 -*-
"""Auto-approved, hash-bound dual Paper portfolios for LLM proposals."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from paper.execution_model import DEFAULT_FEES, EXECUTION_MODEL_VERSION, Bar, portfolio_value
from trading_team.counterfactual import load_execution_bars
from trading_team.execution_bars import load_closing_marks
from trading_team.decision_pipeline import APPROVAL_FIELDS, canonical_plan_hash
from trading_team.execute import SLIPPAGE_RATE, _simulate_buy
from trading_team.entry_contract import ENTRY_CONTRACT_VERSION, entry_contract_errors
from trading_team.paper_plans import ensure_paper_proposals, select_paper_plans
from trading_team.paper_lock import account_locks
from trading_team.state_lock import atomic_write_json, state_lock
from trading_team.schemas import compute_executable, is_sell
from trading_team.exit_contract import EXIT_CONTRACT_VERSION, PORTFOLIO_POLICY_VERSION, exit_contract_errors, simulate_exit
from trading_team.risk_contract import RISK_POLICY_VERSION, validate_risk_document
from trading_team.paper_inventory import (
    INVENTORY_POLICY_VERSION, attach_inventory, available_quantity, consume_position, inventory_basis, read_trades,
    verify_receipt_inventory,
)
from trading_team.protection_contract import PROTECTION_POLICY_VERSION
from trading_team.paper_protection import (
    prepare_protection, read_protection, verify_protection_sources, verify_protection_sessions, verify_protection_reference,
)

TEAM = Path(__file__).resolve().parent
ROOT = TEAM.parent
PAPER_AUTO_START = "2026-09-03"
PAPER_SCHEMA_VERSION = "paper_account.v1"
DEFAULT_DB_PATHS = {
    "gate": ROOT / "paper" / "llm_gate.db",
    "no_gate": ROOT / "paper" / "llm_no_gate.db",
}
COHORT_FILE = "paper/active_cohort.json"


def active_paper_cohort(team: Path = TEAM) -> dict | None:
    path = team.parent / COHORT_FILE
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("Paper cohort configuration must be a regular file")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        name = config["cohort"]
        start = config["start_date"]
        accounts = config["accounts"]
        if (set(config) != {"schema", "cohort", "start_date", "initial_cash", "accounts"}
                or config["schema"] != "paper_cohort.v1" or config["initial_cash"] != 100000
                or not isinstance(name, str) or not isinstance(start, str)
                or date.fromisoformat(start).isoformat() != start or name != f"{start}-forward"
                or not isinstance(accounts, dict) or set(accounts) != {"gate", "no_gate"}):
            raise ValueError("invalid Paper cohort configuration")
        directory = f"paper/cohort_{start}"
        expected = {key: f"{directory}/llm_{key}.db" for key in ("gate", "no_gate")}
        if accounts != expected:
            raise ValueError("Paper cohort account paths do not match its identity")
        for relative in accounts.values():
            target = team.parent / relative
            for item in (target, target.parent, target.parent.parent):
                if item.is_symlink():
                    raise ValueError("Paper cohort path must not contain symlinks")
        return config
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Paper cohort configuration: {exc}") from exc


def paper_db_paths(team: Path = TEAM) -> dict[str, Path]:
    cohort = active_paper_cohort(team)
    if cohort:
        return {name: team.parent / relative for name, relative in cohort["accounts"].items()}
    return {name: team.parent / "paper" / path.name for name, path in DEFAULT_DB_PATHS.items()}


def paper_executable(plan: dict, market_ok: bool, plan_hash: str, risk_verified: bool) -> tuple[bool, str]:
    eligible, reason = compute_executable(plan, market_ok, plan_hash)
    if eligible and not risk_verified:
        return False, "risk contract: structured review evidence missing; no automatic approval"
    return eligible, reason


def load_reliable_marks(team: Path = TEAM, as_of_day: str | None = None,
                        *, max_quote_day: str | None = None,
                        calendar_path: Path | None = None, now: datetime | None = None) -> dict:
    return load_closing_marks(team, as_of_day, max_quote_day=max_quote_day,
                              calendar_path=calendar_path, now=now)


def _sector_map(team: Path) -> dict[str, str]:
    try:
        watchlist = json.loads((team / "watchlist.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(stock["code"]): str(stock.get("sector") or "未分类")
        for stock in watchlist.get("stocks", [])
        if stock.get("code")
    }


def account_snapshot(
    path: Path,
    *,
    portfolio: str | None = None,
    valuation: dict | None = None,
    as_of_day: str | None = None,
    sectors: dict[str, str] | None = None,
) -> dict:
    valuation = valuation or {"source_date": None, "quote_asof": None, "prices": {}}
    prices = valuation.get("prices") or {}
    sectors = sectors or {}
    valuation_day = as_of_day or date.today().isoformat()
    portfolio = portfolio or path.stem
    if not path.exists():
        return {
            "schema_version": PAPER_SCHEMA_VERSION,
            "portfolio": portfolio,
            "exists": False,
            "cash": 100000.0,
            "equity": 100000.0,
            "positions": [],
            "order_counts": {},
            "recent_orders": [],
            "recent_trades": [],
            "cost_exposure": 0.0,
            "market_exposure": 0.0,
            "valuation_equity": 100000.0,
            "quote_asof": valuation.get("quote_asof"),
            "valuation_source_date": valuation.get("source_date"),
            "missing_marks": [],
            "sector_exposure": [],
            "frozen_qty": 0,
            "account": {
                "as_of": None,
                "cash": 100000.0,
                "recorded_equity": 100000.0,
                "valuation_equity": 100000.0,
                "quote_asof": valuation.get("quote_asof"),
            },
            "orders": {"counts": {}, "recent": [], "events": []},
            "valuation_history": [],
        }
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        account = conn.execute(
            "SELECT day,cash,equity,note FROM account ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        positions = [dict(row) for row in conn.execute(
            "SELECT code,qty,avg_cost,buy_day FROM positions ORDER BY code"
        )]
        try:
            basis = inventory_basis(read_trades(conn), valuation_day)
            attach_inventory({row["code"]: row for row in positions}, basis)
            inventory = {"status": "matched_trade_ledger", "execution_certified": False, **basis}
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            inventory = {"status": "unverified", "execution_certified": False, "error": str(exc)}
        try:
            history = read_protection(conn)
            protection = {"status": "matched_receipts", "execution_certified": False,
                          "observed_day": valuation_day,
                          "policy": PROTECTION_POLICY_VERSION, "active": history["active"],
                          "as_of": max(history["days"], default=None),
                          "events": [{"date": day, **event} for day, evidence in history["days"].items()
                                     for event in evidence["events"]][-20:]}
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            protection = {"status": "unverified", "execution_certified": False, "active": None, "error": str(exc)}
        order_counts = {
            row["status"]: int(row["count"])
            for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM pending_orders GROUP BY status"
            )
        }
        orders = [dict(row) for row in conn.execute(
            """
            SELECT execution_day,code,side,qty,planned_price,status,reason,plan_hash,fill_price
            FROM pending_orders ORDER BY id DESC LIMIT 20
            """
        )]
        trades = [dict(row) for row in conn.execute(
            """
            SELECT day,code,side,qty,price,amount,fee,plan_hash
            FROM trades ORDER BY id DESC LIMIT 20
            """
        )]
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        events = (
            [dict(row) for row in conn.execute(
                """
                SELECT portfolio,order_id,event_type,event_at,signal_day,
                       execution_day,code,side,qty,planned_price,status,reason,
                       plan_hash,fee,fill_price,price_type
                FROM order_events ORDER BY id DESC LIMIT 50
                """
            )]
            if "order_events" in tables else []
        )
        history = (
            [dict(row) for row in conn.execute(
                """
                SELECT portfolio,valuation_day,quote_asof,cash,market_value,
                       equity,recorded_equity,cost_exposure,unrealized_pnl,
                       position_count,missing_marks_json,positions_json,
                       plan_hash,source,created_at
                FROM account_valuations
                ORDER BY valuation_day DESC, quote_asof DESC LIMIT 90
                """
            )]
            if "account_valuations" in tables else []
        )
    except sqlite3.OperationalError as exc:
        return {
            "schema_version": PAPER_SCHEMA_VERSION,
            "portfolio": portfolio,
            "exists": True,
            "error": str(exc),
            "cash": None,
            "equity": None,
            "positions": [],
            "order_counts": {},
            "recent_orders": [],
            "recent_trades": [],
            "cost_exposure": 0.0,
            "market_exposure": None,
            "valuation_equity": None,
            "quote_asof": valuation.get("quote_asof"),
            "valuation_source_date": valuation.get("source_date"),
            "missing_marks": [],
            "sector_exposure": [],
            "frozen_qty": 0,
            "account": {
                "as_of": None,
                "cash": None,
                "recorded_equity": None,
                "valuation_equity": None,
                "quote_asof": valuation.get("quote_asof"),
            },
            "orders": {"counts": {}, "recent": [], "events": []},
            "valuation_history": [],
        }
    finally:
        conn.close()
    cash = float(account["cash"]) if account else 100000.0
    quote_asof = valuation.get("quote_asof")
    valuation_error = valuation.get("error")
    state_day = max([account["day"] if account and account["day"] != "init" else "",
                     *(position.get("buy_day") or "" for position in positions)])
    if quote_asof and quote_asof < state_day:
        valuation_error = "quote predates current account state"
    if state_day > valuation_day:
        valuation_error = "account state is after requested valuation date"
    if valuation_error or not quote_asof:
        prices = {}
    cost_exposure = round(sum(
        int(position["qty"]) * float(position["avg_cost"])
        for position in positions
    ), 2)
    missing_marks = []
    known_market_exposure = 0.0
    frozen_qty = 0
    frozen_market_value = 0.0
    sector_values: dict[str, float] = {}
    enriched_positions = []
    for position in positions:
        item = dict(position)
        code = str(item["code"])
        qty = int(item["qty"])
        avg_cost = float(item["avg_cost"])
        mark_price = prices.get(code)
        if (isinstance(mark_price, bool) or not isinstance(mark_price, (float, int))
                or not math.isfinite(mark_price) or mark_price <= 0):
            mark_price = None
        available = available_quantity(item, valuation_day) if inventory["status"] == "matched_trade_ledger" else None
        frozen = qty - available if available is not None else None
        item.update({
            "sector": sectors.get(code, "未分类"),
            "cost_value": round(qty * avg_cost, 2),
            "mark_price": mark_price,
            "mark_evidence": (valuation.get("evidence") or {}).get(code) if mark_price is not None else None,
            "market_value": None,
            "unrealized_pnl": None,
            "frozen_qty": frozen,
            "available_qty": available,
        })
        if frozen:
            frozen_qty += frozen
        if mark_price is None:
            missing_marks.append(code)
        else:
            market_value = round(qty * float(mark_price), 2)
            item["market_value"] = market_value
            item["unrealized_pnl"] = round(market_value - item["cost_value"], 2)
            known_market_exposure += market_value
            sector = item["sector"]
            sector_values[sector] = sector_values.get(sector, 0.0) + market_value
            if frozen:
                frozen_market_value += frozen * float(mark_price)
        enriched_positions.append(item)
    complete = not missing_marks
    market_exposure = round(known_market_exposure, 2) if complete else None
    valuation_equity = round(cash + known_market_exposure, 2) if complete else None
    sector_exposure = [
        {
            "sector": sector,
            "market_value": round(value, 2),
            "equity_pct": (
                round(value / valuation_equity, 6) if valuation_equity else None
            ),
        }
        for sector, value in sorted(
            sector_values.items(), key=lambda item: item[1], reverse=True
        )
    ]
    valuation_history = []
    for raw in history:
        item = dict(raw)
        try:
            item["missing_marks"] = json.loads(item.pop("missing_marks_json"))
            item["positions"] = json.loads(item.pop("positions_json"))
        except (TypeError, json.JSONDecodeError):
            item["missing_marks"] = []
            item["positions"] = []
            item.pop("missing_marks_json", None)
            item.pop("positions_json", None)
        valuation_history.append(item)
    account_info = {
        "as_of": account["day"] if account else None,
        "cash": cash,
        "recorded_equity": float(account["equity"]) if account else 100000.0,
        "valuation_equity": valuation_equity,
        "quote_asof": valuation.get("quote_asof"),
    }
    orders_info = {
        "counts": order_counts,
        "recent": orders,
        "events": events,
    }
    return {
        "schema_version": PAPER_SCHEMA_VERSION,
        "portfolio": portfolio,
        "exists": True,
        "as_of": account["day"] if account else None,
        "cash": cash,
        "equity": float(account["equity"]) if account else 100000.0,
        "recorded_equity": float(account["equity"]) if account else 100000.0,
        "note": account["note"] if account else None,
        "positions": enriched_positions,
        "order_counts": order_counts,
        "recent_orders": orders,
        "recent_trades": trades,
        "cost_exposure": cost_exposure,
        "known_market_exposure": round(known_market_exposure, 2),
        "market_exposure": market_exposure,
        "valuation_equity": valuation_equity,
        "unrealized_pnl": (
            round(known_market_exposure - cost_exposure, 2) if complete else None
        ),
        "market_exposure_pct": (
            round(known_market_exposure / valuation_equity, 6)
            if complete and valuation_equity else None
        ),
        "quote_asof": valuation.get("quote_asof"),
        "valuation_source_date": valuation.get("source_date"),
        "valuation_source": valuation.get("source"),
        "valuation_price_basis": valuation.get("price_basis"),
        "valuation_error": valuation_error,
        "missing_marks": sorted(missing_marks),
        "sector_exposure": sector_exposure,
        "inventory": inventory,
        "protection": protection,
        "frozen_qty": frozen_qty if inventory["status"] == "matched_trade_ledger" else None,
        "frozen_market_value": (round(frozen_market_value, 2)
                                if inventory["status"] == "matched_trade_ledger"
                                and all(row["mark_price"] is not None for row in enriched_positions if row["frozen_qty"])
                                else None),
        "account": account_info,
        "orders": orders_info,
        "valuation_history": valuation_history,
    }


def dual_paper_snapshot(
    db_paths: dict[str, Path] | None = None,
    *,
    team: Path = TEAM,
    as_of_day: str | None = None,
) -> dict:
    paths = db_paths or paper_db_paths(team)
    valuation = load_reliable_marks(team, as_of_day)
    sectors = _sector_map(team)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "valuation": {
            "source": valuation.get("source"),
            "source_date": valuation["source_date"],
            "quote_asof": valuation["quote_asof"],
            "price_basis": valuation.get("price_basis"),
            "error": valuation.get("error"),
        },
        "portfolios": {
            name: account_snapshot(
                paths[name],
                portfolio=name,
                valuation=valuation,
                as_of_day=as_of_day,
                sectors=sectors,
            )
            for name in ("gate", "no_gate")
        },
    }


def record_current_valuations(
    db_paths: dict[str, Path] | None = None,
    *,
    team: Path = TEAM,
    as_of_day: str | None = None,
) -> dict:
    """Append one reliable completed-session valuation without rewriting history."""
    paths = db_paths or paper_db_paths(team)
    with state_lock(team), account_locks(paths.values()):
        return _record_current_valuations(paths, team=team, as_of_day=as_of_day)


def _record_current_valuations(db_paths: dict[str, Path], *, team: Path,
                               as_of_day: str | None) -> dict:
    paths = db_paths or paper_db_paths(team)
    valuation = load_reliable_marks(team, as_of_day)
    quote_asof = valuation.get("quote_asof")
    result = {
        "schema_version": PAPER_SCHEMA_VERSION,
        "quote_asof": quote_asof,
        "portfolios": {},
    }
    for portfolio in ("gate", "no_gate"):
        if not paths[portfolio].exists():
            result["portfolios"][portfolio] = {
                "status": "skipped",
                "reason": "database missing",
            }
            continue
        preflight = account_snapshot(paths[portfolio], portfolio=portfolio,
                                     valuation=valuation, as_of_day=as_of_day)
        if preflight.get("error") or not preflight.get("as_of"):
            raise RuntimeError(f"{portfolio} Paper database invalid: {preflight.get('error') or 'balance missing'}")
        conn = _db(paths[portfolio])
        try:
            # Reserve the writer before reading the account so a fill cannot race the mark.
            conn.execute("BEGIN IMMEDIATE")
            item = account_snapshot(paths[portfolio], portfolio=portfolio,
                                    valuation=valuation, as_of_day=as_of_day)
            if item.get("error") or not item.get("as_of"):
                raise RuntimeError(f"{portfolio} Paper database invalid: {item.get('error') or 'balance missing'}")
            reason = (item.get("valuation_error") or ("reliable quote date missing" if not quote_asof else None)
                      or ("position closing prices missing" if item.get("missing_marks") else None))
            if reason:
                result["portfolios"][portfolio] = {"status": "skipped", "reason": reason}
                continue
            recent_orders = item.get("recent_orders") or []
            receipt = conn.execute(
                "SELECT plan_hash FROM execution_receipts WHERE execution_day<=? ORDER BY execution_day DESC LIMIT 1",
                (quote_asof,),
            ).fetchone()
            plan_hash = receipt[0] if receipt else recent_orders[0].get("plan_hash") if recent_orders else None
            positions = {p["code"]: {"qty": p["qty"], "cost": p["avg_cost"], "buy_day": p["buy_day"]}
                         for p in item["positions"]}
            values = _valuation_row(
                portfolio, quote_asof, item["cash"], positions,
                {code: Bar(**bar) for code, bar in valuation["bars"].items()},
                item["recorded_equity"], plan_hash, source=valuation["source"],
            )
            existing = conn.execute(
                "SELECT * FROM account_valuations WHERE portfolio=? AND valuation_day=? AND quote_asof=?",
                (portfolio, quote_asof, quote_asof),
            ).fetchone()
            if existing:
                old, new = list(existing[:13]), list(values[:13])
                for index in (10, 11):
                    old[index], new[index] = json.loads(old[index]), json.loads(new[index])
                if old != new or existing[13] not in ("execution_close.v3", valuation["source"]):
                    raise ValueError(f"Paper valuation conflict for {portfolio} {quote_asof}; reconcile before retry")
            else:
                conn.execute("INSERT INTO account_valuations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO order_events("
                "portfolio,order_id,event_type,event_at,signal_day,execution_day,"
                "code,side,qty,planned_price,status,reason,plan_hash,fee,fill_price,price_type) "
                "SELECT ?,p.id,'legacy_import',?,p.signal_day,p.execution_day,"
                "p.code,p.side,p.qty,p.planned_price,p.status,p.reason,p.plan_hash,p.fee,p.fill_price,NULL "
                "FROM pending_orders p WHERE NOT EXISTS (SELECT 1 FROM order_events e "
                "WHERE e.portfolio=? AND e.order_id=p.id)",
                (portfolio, values[-1], portfolio),
            )
            conn.commit()
            result["portfolios"][portfolio] = {
                "status": "reused" if existing else "inserted", "equity": values[5],
                "missing_marks": [], "order_events_imported": cursor.rowcount,
            }
        finally:
            conn.close()
    return result


def materialize_paper_decision(day: str, team: Path = TEAM) -> dict:
    from pipeline.policy import require
    require("llm_plan")
    with state_lock(team):
        return _materialize_paper_decision(day, team)


def _materialize_paper_decision(day: str, team: Path) -> dict:
    plan_doc = ensure_paper_proposals(day, team)
    decision = build_paper_decision(plan_doc)
    path = team / "decisions" / day / "paper_decision.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, decision)
    return decision


def build_paper_decision(plan_doc: dict) -> dict:
    """Derive the existing Paper policy without publishing or approving production state."""
    day = plan_doc["date"]
    plan_hash = canonical_plan_hash(plan_doc)
    selected = select_paper_plans(plan_doc)
    risk_verified = validate_risk_document(plan_doc)
    data_asof = str(plan_doc.get("data_asof") or "")
    eligible = day if data_asof and data_asof < day else None
    portfolios = {}
    for name, market_ok in (
        ("gate", bool(plan_doc.get("market_ok", False))),
        ("no_gate", True),
    ):
        plans = []
        for raw in selected:
            plan = dict(raw)
            plan.update({
                "approval": "approved",
                "approval_source": "paper_auto",
                "approved_at": datetime.now().isoformat(timespec="seconds"),
                "approved_plan_hash": plan_hash,
            })
            plan["executable"], plan["blocked_reason"] = paper_executable(
                plan, market_ok, plan_hash, risk_verified,
            )
            if not risk_verified:
                plan.update(approval="pending", approval_source="risk_evidence_missing")
                plan.pop("approved_at")
                plan.pop("approved_plan_hash")
            plans.append(plan)
        portfolios[name] = {
            "market_ok": market_ok,
            "pm_status": "approved" if risk_verified else "pending",
            "plans": plans,
        }
    decision = {
        "date": day,
        "data_asof": plan_doc.get("data_asof"),
        "plan_hash": plan_hash,
        "source_plan": f"outputs/{day}/plan.json",
        "approval_mode": "paper_auto",
        "risk_policy": RISK_POLICY_VERSION,
        "risk_contract": plan_doc.get("risk_contract"),
        "eligible_execution_day": eligible,
        "materialized_at": datetime.now().isoformat(timespec="seconds"),
        "portfolios": portfolios,
    }
    return decision


def _db(path: Path | None) -> sqlite3.Connection:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path if path is not None else ":memory:")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS account(
        day TEXT PRIMARY KEY, cash REAL, equity REAL, note TEXT);
    CREATE TABLE IF NOT EXISTS positions(
        code TEXT PRIMARY KEY, qty INTEGER, avg_cost REAL, buy_day TEXT);
    CREATE TABLE IF NOT EXISTS pending_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_day TEXT, execution_day TEXT, code TEXT, side TEXT, qty INTEGER,
        planned_price REAL, signal_source TEXT, status TEXT, reason TEXT,
        plan_hash TEXT, fee REAL DEFAULT 0, fill_price REAL);
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT, signal_day TEXT, code TEXT, side TEXT, qty INTEGER,
        price REAL, amount REAL, fee REAL, signal_source TEXT,
        plan_hash TEXT, planned_price REAL, price_type TEXT);
    CREATE TABLE IF NOT EXISTS execution_receipts(
        execution_day TEXT PRIMARY KEY,
        plan_hash TEXT NOT NULL,
        result_json TEXT NOT NULL,
        evidence_kind TEXT NOT NULL,
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS order_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portfolio TEXT NOT NULL,
        order_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        event_at TEXT NOT NULL,
        signal_day TEXT,
        execution_day TEXT,
        code TEXT,
        side TEXT,
        qty INTEGER,
        planned_price REAL,
        status TEXT NOT NULL,
        reason TEXT,
        plan_hash TEXT,
        fee REAL,
        fill_price REAL,
        price_type TEXT,
        UNIQUE(portfolio, order_id, event_type));
    CREATE TABLE IF NOT EXISTS account_valuations(
        portfolio TEXT NOT NULL,
        valuation_day TEXT NOT NULL,
        quote_asof TEXT NOT NULL,
        cash REAL NOT NULL,
        market_value REAL,
        equity REAL,
        recorded_equity REAL,
        cost_exposure REAL NOT NULL,
        unrealized_pnl REAL,
        position_count INTEGER NOT NULL,
        missing_marks_json TEXT NOT NULL,
        positions_json TEXT NOT NULL,
        plan_hash TEXT,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(portfolio, valuation_day, quote_asof));
    """)
    if conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0:
        conn.execute("INSERT INTO account VALUES ('init',100000,100000,'初始资金')")
        conn.commit()
    return conn


def _valuation_row(
    portfolio: str,
    valuation_day: str,
    cash: float,
    positions: dict,
    bars: dict[str, Bar],
    recorded_equity: float,
    plan_hash: str | None,
    *,
    source: str = "execution_close.v3",
) -> tuple:
    missing_marks = []
    position_rows = []
    market_value = 0.0
    cost_exposure = 0.0
    for code, position in sorted(positions.items()):
        qty = int(position["qty"])
        avg_cost = float(position["cost"])
        bar = bars.get(code)
        mark_price = float(bar.close) if bar and bar.close is not None else None
        cost_value = qty * avg_cost
        cost_exposure += cost_value
        value = None if mark_price is None else qty * mark_price
        if value is None:
            missing_marks.append(code)
        else:
            market_value += value
        position_rows.append({
            "code": code,
            "qty": qty,
            "avg_cost": round(avg_cost, 6),
            "buy_day": position.get("buy_day"),
            "mark_price": mark_price,
            "market_value": round(value, 2) if value is not None else None,
            "mark_evidence": {"date": valuation_day, "bar": asdict(bar)} if bar else None,
        })
    complete = not missing_marks
    valued_equity = round(cash + market_value, 2) if complete else None
    unrealized_pnl = (
        round(market_value - cost_exposure, 2) if complete else None
    )
    return (
        portfolio,
        valuation_day,
        valuation_day,
        round(cash, 2),
        round(market_value, 2) if complete else None,
        valued_equity,
        round(recorded_equity, 2),
        round(cost_exposure, 2),
        unrealized_pnl,
        len(position_rows),
        json.dumps(sorted(missing_marks), ensure_ascii=False),
        json.dumps(position_rows, ensure_ascii=False, sort_keys=True),
        plan_hash,
        source,
        datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def _execute_portfolio(decision: dict, portfolio: str, db_path: Path,
                       bars: dict[str, Bar], confirm: bool) -> dict:
    conn = _db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        return _execute_portfolio_transaction(decision, portfolio, conn, bars, confirm)
    finally:
        conn.close()


def _execution_inputs(bars: dict[str, Bar], *, review_gate: dict | None = None) -> dict:
    return {
        "model": EXECUTION_MODEL_VERSION,
        "entry_contract": ENTRY_CONTRACT_VERSION,
        "exit_contract": EXIT_CONTRACT_VERSION,
        "portfolio_policy": PORTFOLIO_POLICY_VERSION,
        "risk_policy": RISK_POLICY_VERSION,
        "inventory_policy": INVENTORY_POLICY_VERSION,
        "protection_policy": PROTECTION_POLICY_VERSION,
        "fees": asdict(DEFAULT_FEES),
        "slippage_rate": SLIPPAGE_RATE,
        "bars": {code: asdict(bar) for code, bar in sorted(bars.items())},
        **({"review_gate": review_gate} if review_gate is not None else {}),
    }


def _execution_inputs_hash(inputs: dict) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _verify_empty_settlement(conn: sqlite3.Connection, portfolio: str, day: str) -> None:
    account = conn.execute("SELECT note FROM account WHERE day=?", (day,)).fetchone()
    orders = conn.execute(
        "SELECT COUNT(*) FROM pending_orders WHERE execution_day=?", (day,),
    ).fetchone()[0]
    trades = conn.execute("SELECT COUNT(*) FROM trades WHERE day=?", (day,)).fetchone()[0]
    if account != (f"llm_{portfolio}: 0 fills/0 orders",) or orders or trades:
        raise ValueError(f"Paper legacy evidence conflicts: {portfolio} account/orders/trades")


def import_legacy_empty_receipts(day: str, plan_hash: str, results: dict,
                                 source_run_id: str, team: Path = TEAM) -> None:
    """Recover zero-order receipts from matching ledger and account evidence."""
    with state_lock(team), account_locks(paper_db_paths(team).values()):
        _import_legacy_empty_receipts(day, plan_hash, results, source_run_id, team)


def _import_legacy_empty_receipts(day: str, plan_hash: str, results: dict,
                                  source_run_id: str, team: Path) -> None:
    for portfolio, path in paper_db_paths(team).items():
        result = results.get(portfolio)
        if result != {"status": "succeeded", "orders": 0, "fills": 0}:
            continue
        if not path.is_file():
            raise ValueError(f"Paper legacy evidence missing: {portfolio} database")
        conn = _db(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            receipt = conn.execute(
                "SELECT plan_hash FROM execution_receipts WHERE execution_day=?", (day,),
            ).fetchone()
            if receipt:
                if receipt[0] != plan_hash:
                    raise ValueError(f"Paper legacy evidence conflicts: {portfolio} plan hash")
                continue
            _verify_empty_settlement(conn, portfolio, day)
            conn.execute(
                "INSERT INTO execution_receipts VALUES (?,?,?,?,?)",
                (day, plan_hash, json.dumps({**result, "source_run_id": source_run_id}, sort_keys=True),
                 "legacy_ledger_import", datetime.now().astimezone().isoformat(timespec="seconds")),
            )
            conn.commit()
        finally:
            conn.close()


def _execute_portfolio_transaction(decision: dict, portfolio: str, conn: sqlite3.Connection,
                                   bars: dict[str, Bar], confirm: bool) -> dict:
    doc = decision["portfolios"][portfolio]
    plans = doc["plans"]
    plan_hash = decision["plan_hash"]
    execution_day = decision["eligible_execution_day"]
    signal_day = decision.get("data_asof")
    receipt = conn.execute(
        "SELECT plan_hash,result_json,evidence_kind FROM execution_receipts WHERE execution_day=?",
        (execution_day,),
    ).fetchone()
    if receipt:
        if receipt[0] != plan_hash:
            raise ValueError(f"Paper {portfolio} {execution_day} already settled with another plan")
        previous = json.loads(receipt[1])
        if receipt[2] == "legacy_ledger_import" and previous.get("source_run_id"):
            _verify_empty_settlement(conn, portfolio, execution_day)
            return {"status": "skipped", "orders": 0, "fills": 0,
                    "evidence_kind": "legacy_account_only",
                    "reason": "Historical zero-order account record; execution inputs not recertified"}
        inputs = previous.get("execution_inputs")
        if not isinstance(inputs, dict) or not isinstance(inputs.get("bars"), dict):
            raise ValueError("Paper execution evidence missing; reconcile historical settlement")
        if not inputs["bars"].keys() <= bars.keys():
            raise ValueError("Paper execution evidence bars missing; reconcile before retry")
        current_inputs = _execution_inputs({code: bars[code] for code in inputs["bars"]},
                                           review_gate=decision.get("_review_gate"))
        if (previous.get("execution_inputs_hash") != _execution_inputs_hash(inputs)
                or _execution_inputs_hash(current_inputs) != previous["execution_inputs_hash"]):
            raise ValueError("Paper execution evidence changed; reconcile before retry")
        verify_receipt_inventory(conn, execution_day, previous)
        read_protection(conn)
        return {"status": "skipped", "orders": 0, "fills": 0}
    if conn.execute(
        "SELECT 1 FROM pending_orders WHERE execution_day=? AND plan_hash=? LIMIT 1",
        (execution_day, plan_hash),
    ).fetchone():
        raise ValueError("Paper legacy orders lack execution inputs; reconcile before retry")

    latest_day = conn.execute("SELECT MAX(day) FROM account WHERE day!='init'").fetchone()[0]
    if latest_day and latest_day >= execution_day:
        raise ValueError(f"Paper {portfolio} already settled through {latest_day}; cannot execute {execution_day}")

    cash = conn.execute(
        "SELECT cash FROM account ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    positions = {
        row[0]: {"qty": row[1], "cost": row[2], "buy_day": row[3]}
        for row in conn.execute("SELECT code,qty,avg_cost,buy_day FROM positions")
    }
    equity_before = portfolio_value(cash, positions, bars, price_field="prev_close")
    ledger = read_trades(conn)
    opening_inventory = inventory_basis(ledger, execution_day)
    if any(row["day"] >= execution_day for row in ledger):
        raise ValueError("Paper inventory has same-day or future trades without a settlement receipt")
    attach_inventory(positions, opening_inventory)
    protection_history = read_protection(conn)
    verify_protection_reference(protection_history, bars)
    protection, plans = prepare_protection(plans, positions, protection_history["active"], execution_day,
                                          plan_hash, bars, slippage_rate=SLIPPAGE_RATE)
    sizing_basis = {
        "cash": cash, "equity": equity_before, "price_field": "prev_close",
        "positions": {code: {"qty": pos["qty"], "buy_day": pos["buy_day"], "lots": pos["lots"],
                             "reference_price": bars[code].prev_close}
                      for code, pos in sorted(positions.items())},
    }
    outcomes = []
    trades = []
    buy_cash = cash
    for plan in plans:
        if not is_sell(str(plan.get("direction", ""))):
            continue
        code = str(plan["code"])
        errors = exit_contract_errors(plan)
        if errors or not plan.get("executable"):
            reason = "exit contract: " + "; ".join(errors) if errors else plan.get("blocked_reason", "")
            outcomes.append((code, "sell", 0, None, "blocked", reason, 0.0, None, None))
            continue
        current = positions.get(code, {"qty": 0})
        bar = bars.get(code)
        if bar is None:
            outcomes.append((code, "sell", current["qty"], None, "rejected",
                             "无行情", 0.0, None, None))
            continue
        fill, planned = simulate_exit(plan, current, bar, execution_day, slippage_rate=SLIPPAGE_RATE)
        outcomes.append((code, "sell", fill.qty, planned, fill.status,
                         fill.reject_reason, fill.fee, fill.price or None,
                         fill.price_type))
        if fill.filled:
            cash += fill.amount - fill.fee
            consume_position(current, fill.qty, execution_day)
            if not current["qty"]:
                positions.pop(code)
            trades.append((code, "sell", fill, planned))
    for plan in plans:
        if is_sell(str(plan.get("direction", ""))) or plan.get("direction") in ("持有", "观望", "回避"):
            continue
        code = str(plan["code"])
        errors = entry_contract_errors(plan)
        if plan.get("exit") is not None:
            errors.extend(exit_contract_errors(plan))
        if errors or not plan.get("executable"):
            reason = "entry contract: " + "; ".join(errors) if errors else plan.get("blocked_reason", "")
            outcomes.append((code, "buy", 0, None, "blocked", reason, 0.0, None, None))
            continue
        entries = plan.get("entry") or []
        planned = max(entries) if entries else None
        sizing_price = planned or (bars.get(code) and bars[code].open)
        current = positions.get(code, {"qty": 0, "cost": 0, "buy_day": None})
        target = (int(equity_before * plan["position_pct"] / sizing_price / 100) * 100
                  if sizing_price else 0)
        qty = max(target - current["qty"], 0)
        if qty <= 0:
            continue
        bar = bars.get(code)
        if bar is None:
            outcomes.append((code, "buy", qty, planned, "rejected", "无行情", 0.0, None, None))
            continue
        fill, planned, pre_reason = _simulate_buy(plan, qty, bar)
        if fill is None:
            status = "unfilled" if "未触发" in pre_reason else "rejected"
            outcomes.append((code, "buy", qty, planned, status, pre_reason, 0.0, None, None))
            continue
        if fill.filled and fill.amount + fill.fee > buy_cash:
            affordable = int(buy_cash / (fill.price * 1.001) / 100) * 100
            fill, planned, _ = _simulate_buy(plan, affordable, bar)
        if not fill.filled or fill.amount + fill.fee > buy_cash:
            outcomes.append((code, "buy", qty, planned, fill.status,
                             fill.reject_reason or "现金不足", fill.fee,
                             fill.price or None, fill.price_type))
            continue
        cash -= fill.amount + fill.fee
        buy_cash -= fill.amount + fill.fee
        new_qty = current["qty"] + fill.qty
        positions[code] = {
            "qty": new_qty,
            "cost": ((current["qty"] * current["cost"] + fill.amount + fill.fee)
                     / new_qty),
            "buy_day": execution_day,
        }
        outcomes.append((code, "buy", fill.qty, planned, "filled", "", fill.fee,
                         fill.price, fill.price_type))
        trades.append((code, "buy", fill, planned))

    equity = portfolio_value(cash, positions, bars)
    if not confirm:
        return {"status": "dry-run", "orders": len(outcomes), "fills": len(trades)}
    source = f"llm_{portfolio}"
    event_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for code, side, qty, planned, status, reason, fee, fill_price, price_type in outcomes:
        cursor = conn.execute(
            "INSERT INTO pending_orders(signal_day,execution_day,code,side,qty,"
            "planned_price,signal_source,status,reason,plan_hash,fee,fill_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (signal_day, execution_day, code, side, qty, planned, source, status,
             reason, plan_hash, fee, fill_price),
        )
        conn.execute(
            "INSERT INTO order_events(portfolio,order_id,event_type,event_at,"
            "signal_day,execution_day,code,side,qty,planned_price,status,reason,"
            "plan_hash,fee,fill_price,price_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (portfolio, cursor.lastrowid, "order_finalized", event_at, signal_day,
             execution_day, code, side, qty, planned, status, reason, plan_hash,
             fee, fill_price, price_type),
        )
    conn.executemany(
        "INSERT INTO trades(day,signal_day,code,side,qty,price,amount,fee,"
        "signal_source,plan_hash,planned_price,price_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(execution_day, signal_day, code, side, fill.qty, fill.price, fill.amount,
          fill.fee, source, plan_hash, planned, fill.price_type)
         for code, side, fill, planned in trades],
    )
    conn.execute("DELETE FROM positions")
    conn.executemany(
        "INSERT INTO positions VALUES (?,?,?,?)",
        [(code, pos["qty"], pos["cost"], pos["buy_day"])
         for code, pos in positions.items()],
    )
    conn.execute(
        "INSERT OR REPLACE INTO account VALUES (?,?,?,?)",
        (execution_day, round(cash, 2), round(equity, 2),
         f"{source}: {len(trades)} fills/{len(outcomes)} orders"
         + (f"; protection={PROTECTION_POLICY_VERSION}" if protection["events"] else "")),
    )
    conn.execute(
        "INSERT OR REPLACE INTO account_valuations("
        "portfolio,valuation_day,quote_asof,cash,market_value,equity,"
        "recorded_equity,cost_exposure,unrealized_pnl,position_count,"
        "missing_marks_json,positions_json,plan_hash,source,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _valuation_row(
            portfolio,
            execution_day,
            cash,
            positions,
            bars,
            equity,
            plan_hash,
        ),
    )
    result = {"status": "succeeded", "orders": len(outcomes), "fills": len(trades)}
    inputs = _execution_inputs(bars, review_gate=decision.get("_review_gate"))
    receipt_result = {**result, "protection": protection, "sizing_basis": sizing_basis, "inventory_basis": opening_inventory,
                      "inventory_closing": inventory_basis(read_trades(conn), execution_day), "execution_inputs": inputs,
                      "execution_inputs_hash": _execution_inputs_hash(inputs)}
    conn.execute(
        "INSERT INTO execution_receipts VALUES (?,?,?,?,?)",
        (execution_day, plan_hash, json.dumps(receipt_result, sort_keys=True), "execution", event_at),
    )
    conn.commit()
    return result


def run_paper_day(day: str, team: Path = TEAM,
                  db_paths: dict[str, Path] | None = None,
                  confirm: bool = True) -> dict:
    from pipeline.policy import require
    require("llm_execution")
    cohort = active_paper_cohort(team)
    if cohort and (day < cohort["start_date"] or (db_paths is not None and db_paths != paper_db_paths(team))):
        raise ValueError("Paper day or account paths outside active cohort")
    paths = db_paths or paper_db_paths(team)
    with state_lock(team), account_locks(paths.values()):
        return _run_paper_day(day, team=team, db_paths=paths, confirm=confirm)


def _validate_paper_decision(decision: dict, plan_doc: dict, day: str) -> None:
    """A copied top-level hash cannot authorize altered decision content."""
    selected = select_paper_plans(plan_doc)
    risk_verified = validate_risk_document(plan_doc)
    approval_fields = {*APPROVAL_FIELDS, "approval_source"}
    facts = lambda row: {key: value for key, value in row.items() if key not in approval_fields}
    data_asof = str(plan_doc.get("data_asof") or "")
    eligible_day = day if data_asof and data_asof < day else None
    if (decision.get("date") != day or plan_doc.get("date") != day
            or decision.get("data_asof") != plan_doc.get("data_asof")
            or decision.get("eligible_execution_day") != eligible_day
            or decision.get("approval_mode") != "paper_auto"
            or decision.get("risk_policy") != RISK_POLICY_VERSION
            or decision.get("risk_contract") != plan_doc.get("risk_contract")):
        raise ValueError("Paper decision date or approval mode conflicts; rematerialize")
    for name, market_ok in (("gate", bool(plan_doc.get("market_ok"))), ("no_gate", True)):
        portfolio = decision.get("portfolios", {}).get(name, {})
        plans = portfolio.get("plans", [])
        if (portfolio.get("market_ok") is not market_ok or len(plans) != len(selected)
                or [facts(row) for row in plans] != [facts(row) for row in selected]):
            raise ValueError(f"Paper decision {name} content conflicts with plan; rematerialize")
        for plan in plans:
            if (plan.get("approval") != ("approved" if risk_verified else "pending")
                    or plan.get("approval_source") != ("paper_auto" if risk_verified else "risk_evidence_missing")
                    or plan.get("approved_plan_hash") != (decision["plan_hash"] if risk_verified else None)
                    or plan.get("executable") is not paper_executable(plan, market_ok, decision["plan_hash"], risk_verified)[0]):
                raise ValueError(f"Paper decision {name} approval or execution policy conflicts; rematerialize")


def _run_paper_day(day: str, team: Path, db_paths: dict[str, Path], confirm: bool) -> dict:
    decision_path = team / "decisions" / day / "paper_decision.json"
    decision = (json.loads(decision_path.read_text(encoding="utf-8"))
                if decision_path.exists() else materialize_paper_decision(day, team))
    plan_doc = json.loads(
        (team / "outputs" / day / "plan.json").read_text(encoding="utf-8")
    )
    if decision.get("plan_hash") != canonical_plan_hash(plan_doc):
        raise ValueError("Paper 审批版本已失效，必须重新物化")
    _validate_paper_decision(decision, plan_doc, day)
    execution_day = decision.get("eligible_execution_day")
    if day < PAPER_AUTO_START:
        return {"status": "skipped", "reason": "早于 Paper 自动运行启用日"}
    if not execution_day:
        return {"status": "skipped", "reason": "计划使用当日收盘数据，禁止回填成交"}
    from trading_team.multica_reviews import require_reviews
    decision["_review_gate"] = require_reviews(day, decision["plan_hash"], team=team)
    plans = decision["portfolios"]["no_gate"]["plans"]
    codes = {str(plan["code"]) for plan in plans}
    paths = db_paths or paper_db_paths(team)
    histories = {}
    for name, path in paths.items():
        if path.exists():
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                codes.update(row[0] for row in conn.execute("SELECT code FROM positions"))
                has_receipts = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_receipts'",
                ).fetchone()
                receipt = conn.execute(
                    "SELECT result_json FROM execution_receipts WHERE execution_day=?", (execution_day,),
                ).fetchone() if has_receipts else None
                if has_receipts:
                    history = read_protection(conn)
                    histories[name] = history
                    verify_protection_sources(history, team, name)
                    if any(e["closing"] for e in history["days"].values()):
                        from trading_team.collect import load_trading_days
                        from trading_team.execution_bars import DEFAULT_TRADING_CALENDAR
                        verify_protection_sessions(history, load_trading_days(DEFAULT_TRADING_CALENDAR), execution_day)
                if receipt:
                    previous_inputs = json.loads(receipt[0]).get("execution_inputs") or {}
                    codes.update(previous_inputs.get("bars") or {})
            finally:
                conn.close()
    bars = load_execution_bars(execution_day, sorted(codes), team)
    for history in histories.values():
        if execution_day > max(history["days"], default=""):
            verify_protection_reference(history, bars)
    return {
        name: _execute_portfolio(decision, name, paths[name], bars, confirm)
        for name in ("gate", "no_gate")
    }
