"""Read-only console facts. Execution status is not artifact verification."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_team.collect import DEFAULT_TRADING_CALENDAR, load_trading_days
from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.risk_contract import risk_review_summary
from trading_team.kimi_daily import _validate_manifest, inspect_generation
from trading_team.paper_pipeline import account_snapshot, load_reliable_marks, paper_db_paths
from trading_team.run_ledger import RunLedger
from trading_team.approvals import status as approval_status
from trading_team.notification_outbox import summary as notification_summary

TEAM = Path(__file__).resolve().parent
SHANGHAI = ZoneInfo("Asia/Shanghai")
READ_ERRORS = (OSError, ValueError, TypeError, KeyError, sqlite3.Error)


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be an object")
    return value


def _latest_day(root: Path) -> str | None:
    days = []
    for path in root.iterdir() if root.exists() else []:
        if path.is_dir():
            try:
                date.fromisoformat(path.name)
                days.append(path.name)
            except ValueError:
                continue
    return max(days) if days else None


def _calendar(path: Path, now: datetime) -> dict:
    try:
        sessions = sorted(load_trading_days(path))
        today = now.date().isoformat()
        if not sessions or not sessions[0] <= today <= sessions[-1]:
            raise ValueError("trading calendar does not cover today")
        completed = [day for day in sessions if day < today or (
            day == today and now.time() >= time(15, 10))]
        if not completed:
            raise ValueError("no completed session in calendar")
        return {"status": "available", "expected_session": completed[-1]}
    except READ_ERRORS as exc:
        return {"status": "unavailable", "expected_session": None, "error": str(exc)}


def _freshness(asof: str | None, expected: str | None) -> str:
    if not asof or not expected:
        return "unknown"
    date.fromisoformat(asof)
    return "current" if asof == expected else "stale" if asof < expected else "future"


def _binding(path: Path, day: str, plan_hash: str) -> dict:
    if not path.exists():
        return {"status": "missing", "approved_count": 0}
    try:
        decision = _read(path)
        if decision.get("date") != day or decision.get("plan_hash") != plan_hash:
            return {"status": "mismatch", "approved_count": 0}
        count = sum(p.get("approval") in ("approved", "reduced")
                    and p.get("approved_plan_hash") == plan_hash
                    for p in decision.get("plans", []))
        return {"status": "bound", "approved_count": count}
    except (AttributeError, *READ_ERRORS) as exc:
        return {"status": "invalid", "approved_count": 0, "error": str(exc)}


def _research(team: Path, ledger: RunLedger, today: str) -> dict:
    day = _latest_day(team / "outputs")
    result = {"date": day, "status": "missing", "plan_hash": None,
              "risk_review": {"status": "missing", "reviewed_count": None},
              "generation": {"status": "missing"}, "decision": {"status": "missing"},
              "approval_audit": {"status": "missing", "event_count": 0},
              "paper_decision": {"status": "missing"}}
    if not day:
        return result
    try:
        plan = _read(team / "outputs" / day / "plan.json")
        if plan.get("date") != day or day > today:
            raise ValueError("plan date is inconsistent or in the future")
        plan_hash = canonical_plan_hash(plan)
        result.update(status="available", plan_hash=plan_hash,
                      data_asof=plan.get("data_asof"), market_ok=plan.get("market_ok"),
                      risk_review=risk_review_summary(plan))
        for key, name in (("decision", "decision.json"), ("paper_decision", "paper_decision.json")):
            result[key] = _binding(team / "decisions" / day / name, day, plan_hash)
        audit = approval_status(day, team=team)
        result["approval_audit"] = {key: value for key, value in audit.items() if key != "events"}
        if (team / "outputs" / day / "generation.json").exists():
            try:
                evidence = inspect_generation(team, day, ledger)
                publisher = evidence["publisher"]
                acknowledged = (publisher["status"] == "succeeded"
                                and publisher["output_hash"] == evidence["receipt_hash"])
                result["generation"] = {
                    "status": "verified" if acknowledged else "recovery_required",
                    "run_id": publisher["run_id"],
                }
            except (AttributeError, *READ_ERRORS) as exc:
                result["generation"] = {"status": "invalid", "error": str(exc)}
    except (AttributeError, *READ_ERRORS) as exc:
        result.update(status="invalid", error=str(exc), plan_hash=None)
    return result


def _data(team: Path, expected: str | None, today: str) -> dict:
    day = _latest_day(team / "context")
    result = {"date": day, "status": "missing", "manifest_ok": None,
              "data_asof": None, "freshness": "unknown"}
    if day:
        try:
            manifest = _read(team / "context" / day / "manifest.json")
            if day > today:
                raise ValueError("context date is in the future")
            _validate_manifest(manifest, day, team)
            result.update(status="available", manifest_ok=True,
                          data_asof=manifest["latest_trade_date"],
                          freshness=_freshness(manifest["latest_trade_date"], expected))
        except (AttributeError, *READ_ERRORS) as exc:
            result.update(status="invalid", manifest_ok=False, error=str(exc))
    return result


def _account(path: Path, name: str, marks: dict, today: str, expected: str | None) -> dict:
    result = {"status": "missing", "cash": None, "valuation_equity": None,
              "position_count": None, "order_counts": {}, "account_asof": None,
              "quote_asof": None, "freshness": "unknown", "missing_marks": []}
    if not path.exists():
        return result
    try:
        account = account_snapshot(path, portfolio=name, valuation=marks, as_of_day=today)
        if account.get("error") or not account.get("account", {}).get("as_of"):
            raise ValueError(account.get("error") or "account has no balance record")
        result.update(status="available", cash=account["cash"],
                      valuation_equity=account["valuation_equity"],
                      position_count=len(account["positions"]), order_counts=account["order_counts"],
                      account_asof=account["account"]["as_of"], quote_asof=account["quote_asof"],
                      freshness=_freshness(account["quote_asof"], expected),
                      valuation_error=account.get("valuation_error"),
                      valuation_source=account.get("valuation_source"),
                      valuation_price_basis=account.get("valuation_price_basis"),
                      missing_marks=account["missing_marks"])
    except READ_ERRORS as exc:
        result.update(status="invalid", error=str(exc))
    return result


def operational_snapshot(*, team: Path = TEAM, calendar_path: Path = DEFAULT_TRADING_CALENDAR,
                         now: datetime | None = None) -> dict:
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    today = now.date().isoformat()
    calendar = _calendar(calendar_path, now)
    expected = calendar["expected_session"]
    ledger = RunLedger(team / ".runs/ledger.db", readonly=True)
    result = {"schema": "operational_snapshot.v1", "generated_at": now.isoformat(timespec="seconds"),
              "calendar": calendar, "data": _data(team, expected, today),
              "research": _research(team, ledger, today), "runs": [], "ledger_status": "missing",
              "notifications": notification_summary(team=team)}
    if ledger.path.exists():
        try:
            result.update(runs=ledger.list_runs(limit=8), ledger_status="available")
        except READ_ERRORS as exc:
            result.update(ledger_status="invalid", ledger_error=str(exc))
    marks = load_reliable_marks(team, today, max_quote_day=expected, calendar_path=calendar_path, now=now)
    result["portfolios"] = {
        name: _account(path, name, marks, today, expected)
        for name, path in paper_db_paths(team).items()
    }
    return result
