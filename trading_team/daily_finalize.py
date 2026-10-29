# -*- coding: utf-8 -*-
"""Daily plan materialization and completed-session counterfactual output."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from trading_team import db
from trading_team.collect import DEFAULT_TRADING_CALENDAR, load_trading_days
from trading_team.counterfactual import write_counterfactual
from trading_team.execution_bars import SCHEMA as EXECUTION_DATA_SCHEMA
from trading_team.decision_pipeline import canonical_plan_hash, materialize_decision
from trading_team.paper_pipeline import (
    _execution_inputs, active_paper_cohort, import_legacy_empty_receipts, materialize_paper_decision,
    paper_db_paths, run_paper_day,
)
from trading_team.paper_plans import ensure_paper_proposals
from trading_team.run_ledger import RunLedger
from trading_team.state_lock import state_lock
from trading_team.paper_inventory import INVENTORY_POLICY_VERSION, verify_receipt_inventory
from trading_team.protection_contract import PROTECTION_POLICY_VERSION
from trading_team.paper_protection import read_protection, verify_protection_sources, verify_protection_sessions

TEAM = Path(__file__).resolve().parent
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _session_state(day: str, now: datetime, calendar_path: Path) -> dict:
    try:
        calendar_hash = hashlib.sha256(calendar_path.read_bytes()).hexdigest()
        sessions = load_trading_days(calendar_path)
        if not sessions or not min(sessions) <= day <= max(sessions):
            raise ValueError(f"calendar does not cover {day}")
    except (OSError, ValueError, TypeError, EOFError) as exc:
        raise ValueError(f"A-share calendar unavailable: {exc}") from exc
    current = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    is_session = day in sessions
    completed = is_session and (
        day < current.date().isoformat()
        or (day == current.date().isoformat() and current.time() >= time(15, 10))
    )
    return {
        "date": day, "is_session": is_session, "completed": completed,
        "calendar_hash": calendar_hash,
        "reason": "completed" if completed else "session not completed" if is_session else "exchange closed",
    }


def _paper_evidence(day: str, plan_hash: str, team: Path) -> dict:
    from trading_team.multica_reviews import require_reviews
    review_gate = require_reviews(day, plan_hash, team=team)
    evidence = {}
    for portfolio, path in paper_db_paths(team).items():
        if not path.is_file():
            raise ValueError(f"Paper evidence missing: {portfolio} database")
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("BEGIN")
            receipt = conn.execute(
                "SELECT * FROM execution_receipts WHERE execution_day=? AND plan_hash=?",
                (day, plan_hash),
            ).fetchall()
            account = conn.execute("SELECT * FROM account WHERE day=?", (day,)).fetchall()
            if not receipt or not account:
                raise ValueError(f"Paper evidence missing: {portfolio} receipt/account for {day}")
            result = json.loads(receipt[0][2])
            if result.get("execution_inputs", {}).get("review_gate") != review_gate:
                raise ValueError("Paper evidence changed: settlement review version differs")
            if result.get("execution_inputs", {}).get("inventory_policy") == INVENTORY_POLICY_VERSION:
                verify_receipt_inventory(conn, day, result)
            if result.get("execution_inputs", {}).get("protection_policy") == PROTECTION_POLICY_VERSION:
                history = read_protection(conn)
                verify_protection_sources(history, team, portfolio)
                if any(e["closing"] for e in history["days"].values()):
                    verify_protection_sessions(history, load_trading_days(DEFAULT_TRADING_CALENDAR))
            facts = {"receipt": receipt, "account": account}
            for table, column in (("pending_orders", "execution_day"), ("trades", "day"),
                                  ("order_events", "execution_day")):
                facts[table] = conn.execute(
                    f"SELECT * FROM {table} WHERE {column}=? AND plan_hash=? ORDER BY id",
                    (day, plan_hash),
                ).fetchall()
            evidence[portfolio] = _digest(facts)
        except sqlite3.Error as exc:
            raise ValueError(f"Paper evidence unreadable: {portfolio}: {exc}") from exc
        finally:
            conn.close()
    return evidence


def _step_evidence(name: str, result: dict, day: str, plan_hash: str, team: Path) -> dict:
    paths = []
    evidence = {"files": {}}
    if name == "review_gate":
        from trading_team.multica_reviews import require_reviews
        evidence["review_gate"] = require_reviews(day, plan_hash, team=team)
    elif name == "materialize":
        paths = [team / "decisions" / day / filename
                 for filename in ("decision.json", "paper_decision.json")]
    elif name == "counterfactual":
        paths = [team / "outputs" / day / filename
                 for filename in ("counterfactual.json", "counterfactual.md")]
        paths.append(team / "replay_data" / f"{day}.json")
        if result.get("continuous_report_hash"):
            from trading_team.continuous_counterfactual import _hash, runtime_fingerprints
            series_path = team / "outputs" / day / "continuous_counterfactual.json"
            series = json.loads(series_path.read_text(encoding="utf-8"))
            if (series.get("report_hash") != result["continuous_report_hash"]
                    or _hash({key:value for key,value in series.items() if key != "report_hash"}) != series["report_hash"]):
                raise ValueError("continuous counterfactual report version changed")
            expected_sources = set()
            for row in series["rows"]:
                session = date.fromisoformat(row["date"]).isoformat()
                if session != row["date"]:
                    raise ValueError("continuous counterfactual session invalid")
                expected_sources.update((f"outputs/{session}/plan.json", f"replay_data/{session}.json"))
            if set(series["inputs"]["files"]) != expected_sources:
                raise ValueError("continuous counterfactual source paths invalid")
            paths.extend((series_path, series_path.with_suffix(".md")))
            evidence["series_runtime"] = runtime_fingerprints()
            evidence["series_sources"] = {
                relative: hashlib.sha256((team / relative).read_bytes()).hexdigest() if (team / relative).exists() else None
                for relative in series["inputs"]["files"]}
    elif name == "paper" and result.get("status") != "skipped":
        evidence["paper"] = _paper_evidence(day, plan_hash, team)
        paths = [team / "replay_data" / f"{day}.json"]
    evidence["files"] = {
        str(path.relative_to(team)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    return evidence


def _step_verified(step: dict | None, name: str, day: str, plan_hash: str, team: Path) -> bool:
    if not step or step["status"] != "succeeded" or "evidence" not in step["metadata"]:
        return False
    result = step["metadata"].get("result") or {}
    try:
        return step["metadata"]["evidence"] == _step_evidence(name, result, day, plan_hash, team)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def materialize_day(day: str, team: Path = TEAM) -> dict:
    with state_lock(team):
        return _materialize_day(day, team)


def _materialize_day(day: str, team: Path) -> dict:
    ensure_paper_proposals(day, team)
    decision = materialize_decision(day, team)
    paper_decision = materialize_paper_decision(day, team)
    if team == TEAM:
        db.init_db()
        conn = db.get_conn()
        db.sync_decision(decision, conn)
        conn.close()
    return {
        "date": day,
        "plan_hash": decision["plan_hash"],
        "decision": "ok",
        "paper_decision": paper_decision["approval_mode"],
    }


def finalize_day(day: str, team: Path = TEAM, replay: bool = True,
                 paper: bool = False) -> dict:
    return run_finalize_workflow(day, team=team, replay=replay, paper=paper)


def run_finalize_workflow(
    day: str,
    *,
    team: Path = TEAM,
    replay: bool = True,
    paper: bool = False,
    trigger_source: str = "manual",
    ledger: RunLedger | None = None,
    now: datetime | None = None,
    calendar_path: Path | None = None,
) -> dict:
    """Run deterministic finalization with versioned, failed-step recovery."""
    from pipeline.policy import require
    require("llm_plan")
    cohort = active_paper_cohort(team) if paper else None
    if cohort and day < cohort["start_date"]:
        raise ValueError("Paper day outside active cohort; legacy account remains read-only")
    with state_lock(team):
        return _run_finalize_workflow(
            day, team=team, replay=replay, paper=paper, trigger_source=trigger_source,
            ledger=ledger, now=now, calendar_path=calendar_path,
        )


def _run_finalize_workflow(
    day: str, *, team: Path, replay: bool, paper: bool, trigger_source: str,
    ledger: RunLedger | None, now: datetime | None, calendar_path: Path | None,
) -> dict:
    ledger = ledger or RunLedger(team / ".runs" / "ledger.db")
    plan_doc = {}
    plan_hash = None
    session = {}
    preflight_error = None
    try:
        date.fromisoformat(day)
        plan_doc = (json.loads((team / "outputs" / day / "plan.json").read_text(encoding="utf-8"))
                    if paper else ensure_paper_proposals(day, team))
        if plan_doc.get("date") != day:
            raise ValueError("plan date does not match business date")
        if paper and "paper_proposals" not in plan_doc:
            raise ValueError("Paper plan proposals missing; publish a complete verified plan")
        plan_hash = canonical_plan_hash(plan_doc)
        session = _session_state(day, now or datetime.now(SHANGHAI), calendar_path or DEFAULT_TRADING_CALENDAR)
    except (OSError, ValueError, TypeError) as exc:
        preflight_error = exc
    requested = {"replay": replay, "paper": paper}
    replay = replay and session.get("completed", False)
    paper = paper and session.get("completed", False)
    input_hash = _digest({"schema": 5, "execution_data_schema": EXECUTION_DATA_SCHEMA,
                          "execution_policy": _execution_inputs({}),
                          "plan_hash": plan_hash, "replay": replay,
                          "paper": paper, "session": session, "requested": requested})
    completed = ledger.find_successful_run("daily_finalize", day, input_hash)
    required = ["session_gate", "materialize"]
    if replay:
        required.append("counterfactual")
    if paper:
        required.extend(("review_gate", "paper"))
    if completed and not preflight_error and all(
        _step_verified(ledger.latest_step(completed["run_id"], name), name, day, plan_hash, team)
        for name in required
    ):
        return {
            "date": day,
            "status": "reused",
            "run_id": completed["run_id"],
            "plan_hash": plan_hash,
        }

    run_id = ledger.start_run(
        "daily_finalize",
        day,
        trigger_source=trigger_source,
        input_hash=input_hash,
        metadata={"requested": requested, "replay": replay, "paper": paper, "session": session},
    )

    def run_step(name: str, callback, artifact_path: Path | None = None):
        previous = next((step for step in reversed(ledger.get_run(run_id)["steps"])
                         if step["step_name"] == name and step["status"] == "succeeded"), None)
        if previous is None and completed:
            previous = ledger.latest_step(completed["run_id"], name)
        verified = _step_verified(previous, name, day, plan_hash, team)
        if verified and previous["run_id"] == run_id:
            return previous["metadata"]["result"]
        step_id = ledger.start_step(run_id, name, input_hash=input_hash)
        try:
            if verified:
                ledger.finish_step(
                    step_id, output_hash=previous["output_hash"], artifact_path=previous["artifact_path"],
                    metadata={**previous["metadata"], "reused_from_run": previous["run_id"]},
                )
                return previous["metadata"]["result"]
            if name == "paper" and previous and previous["metadata"].get("evidence", {}).get("paper"):
                raise ValueError("Paper evidence changed after settlement; reconcile account before retry")
            result = callback()
            evidence = _step_evidence(name, result, day, plan_hash, team)
            output_hash = _digest(result)
            if artifact_path and artifact_path.exists():
                output_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            ledger.finish_step(
                step_id,
                output_hash=output_hash,
                artifact_path=(str(artifact_path.relative_to(team.parent))
                               if artifact_path and artifact_path.exists() else None),
                metadata={"result": result, "evidence": evidence},
            )
            return result
        except Exception as exc:
            ledger.finish_step(
                step_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    def skipped_step(name: str, reason: str) -> dict:
        previous = ledger.latest_step(run_id, name)
        if previous and previous["status"] == "skipped":
            return previous["metadata"].get("result") or {"status": "skipped", "reason": reason}
        step_id = ledger.start_step(run_id, name, input_hash=input_hash)
        result = {"status": "skipped", "reason": reason}
        ledger.finish_step(step_id, status="skipped", metadata={"result": result})
        return result

    def execute_paper():
        legacy = ledger.find_successful_run(
            "daily_finalize", day, _digest({"plan_hash": plan_hash, "replay": True, "paper": True}),
        )
        if legacy and legacy["plan_hash"] == plan_hash:
            proof = ledger.latest_step(legacy["run_id"], "paper")
            if proof and proof["status"] == "succeeded":
                import_legacy_empty_receipts(
                    day, plan_hash, proof["metadata"].get("result") or {}, legacy["run_id"], team,
                )
        return run_paper_day(day, team=team)

    try:
        def verify_session():
            if preflight_error:
                raise preflight_error
            return session

        run_step("session_gate", verify_session)
        from trading_team.multica_reviews import require_reviews
        if paper:
            counterfactual = (run_step("counterfactual", lambda: write_counterfactual(day, team),
                                       team / "outputs" / day / "counterfactual.json") if replay else None)
            review_gate = run_step("review_gate", lambda: require_reviews(day, plan_hash, team=team))
        materialized = run_step(
            "materialize",
            lambda: materialize_day(day, team),
            team / "decisions" / day / "decision.json",
        )
        if not paper:
            counterfactual = (run_step("counterfactual", lambda: write_counterfactual(day, team),
                                       team / "outputs" / day / "counterfactual.json") if replay else None)
            review_gate = skipped_step("review_gate", "Paper execution not requested or session incomplete")
        if counterfactual is None:
            counterfactual = skipped_step("counterfactual", session["reason"] if requested["replay"] else "disabled")
        paper_result = (
            run_step("paper", execute_paper)
            if paper else skipped_step("paper", session["reason"] if requested["paper"] else "disabled")
        )
        result = {
            "date": day,
            "status": "succeeded",
            "run_id": run_id,
            "plan_hash": plan_hash,
            "session": session,
            "materialize": materialized,
            "review_gate": review_gate,
            "counterfactual": counterfactual,
            "paper": paper_result,
        }
        ledger.finish_run(
            run_id,
            status="succeeded",
            data_asof=plan_doc.get("data_asof"),
            plan_hash=plan_hash,
            metadata=result,
        )
        return result
    except Exception as exc:
        ledger.finish_run(
            run_id,
            status="failed",
            data_asof=plan_doc.get("data_asof"),
            plan_hash=plan_hash,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def finalize_available_tracked(
    team: Path = TEAM,
    today: date | None = None,
    trigger_source: str = "scheduler",
    ledger: RunLedger | None = None,
    now: datetime | None = None,
    calendar_path: Path | None = None,
    focus_day: str | None = None,
) -> list[dict]:
    current = now or (datetime.combine(today, time.min, SHANGHAI) if today else datetime.now(SHANGHAI))
    ledger = ledger or RunLedger(team / ".runs" / "ledger.db")
    results = []
    cohort = active_paper_cohort(team)
    days = sorted(path.parent.name for path in (team / "outputs").glob("????-??-??/plan.json"))
    if focus_day is not None:
        date.fromisoformat(focus_day)
        sessions = load_trading_days(calendar_path or DEFAULT_TRADING_CALENDAR)
        if not sessions or not min(sessions) <= focus_day <= max(sessions):
            raise ValueError(f"calendar does not cover {focus_day}")
        previous = sorted(day for day in sessions if day < focus_day)
        days = sorted({focus_day, *previous[-1:]})
    for day in days:
        if cohort and day < cohort["start_date"]:
            results.append({"date": day, "status": "skipped", "reason": "outside active Paper cohort"})
            continue
        if not (team / "outputs" / day / "plan.json").is_file():
            results.append({"date": day, "status": "skipped", "reason": "no plan exists for session"})
            continue
        try:
            results.append(run_finalize_workflow(
                day,
                team=team,
                replay=True,
                paper=True,
                trigger_source=trigger_source,
                ledger=ledger,
                now=current,
                calendar_path=calendar_path,
            ))
        except (OSError, ValueError, RuntimeError, sqlite3.Error, requests.RequestException) as exc:
            results.append({"date": day, "status": "failed", "reason": str(exc)})
    return results


def finalize_available(team: Path = TEAM, today: date | None = None) -> list[dict]:
    return finalize_available_tracked(team=team, today=today)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("day", nargs="?")
    parser.add_argument("--focus-date", help="Finalize this date and the prior trading session only")
    args = parser.parse_args()
    if args.day and args.focus_date:
        parser.error("day and --focus-date are mutually exclusive")
    result = (finalize_available_tracked(focus_day=args.focus_date) if args.focus_date
              else run_finalize_workflow(args.day) if args.day else finalize_available_tracked())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if any(item.get("status") == "failed" for item in (result if isinstance(result, list) else [result])):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
