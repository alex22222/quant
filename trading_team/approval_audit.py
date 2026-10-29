"""Approval events committed with the decision; read-only projection verification."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

EVENT_SCHEMA = "approval_event.v1"
PROJECTION_SCHEMA = "approval_projection.v1"
REQUEST_FIELDS = ("date", "code", "name", "decision", "plan_hash", "note", "source")


def event_hash(event: dict) -> str:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def validate_history(doc: dict) -> list[dict]:
    if not isinstance(doc, dict) or not isinstance(doc.get("plans", []), list):
        raise ValueError("approval decision shape invalid")
    if any(not isinstance(plan, dict) for plan in doc.get("plans", [])):
        raise ValueError("approval plan shape invalid")
    if "approval_events" not in doc and "approval_projection" not in doc:
        if any("approval_event_id" in p for p in doc.get("plans", [])):
            raise ValueError("approval event history missing")
        return []
    events = doc.get("approval_events")
    if not isinstance(events, list) or not events:
        raise ValueError("approval event history invalid")
    previous, ids = None, set()
    for event in events:
        if (not isinstance(event, dict) or event.get("schema") != EVENT_SCHEMA
                or not all(isinstance(event.get(key), str) for key in (*REQUEST_FIELDS, "decided_at", "event_id"))
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", event["event_id"])
                or event["event_id"] in ids or event["date"] != doc.get("date")
                or event["decision"] not in ("approved", "rejected", "reduced")
                or not re.fullmatch(r"[a-f0-9]{64}", event["plan_hash"])
                or event.get("previous_event_hash") != previous or event.get("event_hash") != event_hash(event)):
            raise ValueError("approval event chain invalid")
        previous = event["event_hash"]
        ids.add(event["event_id"])
    projection = doc.get("approval_projection")
    if (not isinstance(projection, dict) or projection.get("schema") != PROJECTION_SCHEMA
            or projection.get("status") not in ("pending", "complete")
            or projection.get("through_event_hash") != previous):
        raise ValueError("approval projection binding invalid")
    destinations = projection.get("destinations", {})
    if (not isinstance(destinations, dict)
            or any(value not in ("synced", "absent", "not_configured") for value in destinations.values())
            or (projection["status"] == "complete" and set(destinations) != {"team_db", "agent_md"})):
        raise ValueError("approval projection destinations invalid")
    latest = {event["code"]: event for event in events if event["plan_hash"] == doc.get("plan_hash")}
    plans = {str(p.get("code")): p for p in doc.get("plans", [])}
    for code, event in latest.items():
        plan = plans.get(code, {})
        expected = {"approval": event["decision"], "approved_plan_hash": event["plan_hash"],
                    "approval_note": event["note"], "approved_at": event["decided_at"],
                    "approval_event_id": event["event_id"]}
        if any(plan.get(key) != value for key, value in expected.items()):
            raise ValueError("approval event and current decision disagree")
    for code, plan in plans.items():
        if "approval_event_id" in plan and code not in latest:
            raise ValueError("approval event does not bind current plan")
    return events


def entry_for(event: dict, doc: dict) -> dict:
    plan = next((p for p in doc.get("plans", []) if str(p.get("code")) == event["code"]), {})
    current = (event["plan_hash"] == doc.get("plan_hash")
               and plan.get("approval_event_id") == event["event_id"])
    return {key: event[key] for key in (*REQUEST_FIELDS, "decided_at", "event_id", "event_hash")} | {
        "master_synced": True, "superseded": not current,
        "executable": bool(plan.get("executable")) if current else False,
        "blocked_reason": plan.get("blocked_reason") if current else "approval superseded",
    }


def projected_entries(doc: dict) -> list[dict]:
    latest = {(event["plan_hash"], event["code"]): event for event in validate_history(doc)}
    return [entry_for(event, doc) for event in latest.values()]


def marker(event: dict) -> str:
    return f"<!-- approval-event:{event['event_id']} date:{event['date']} -->"


def database_event(event: dict) -> tuple:
    return tuple(event[key] for key in ("date", "code", "decision", "decided_at", "source", "note",
                                        "plan_hash", "event_id", "event_hash"))


def validate_decision_facts(doc: dict, plan: dict) -> None:
    from trading_team.decision_pipeline import normalized_plan_document
    expected = normalized_plan_document(plan)["proposals"]
    actual = normalized_plan_document({"proposals": doc.get("plans", [])})["proposals"]
    if actual != expected or doc.get("market_ok") != bool(plan.get("market_ok", False)):
        raise ValueError("approval decision facts differ from source plan")


def inspect_projection(doc: dict, team: Path) -> dict:
    events = validate_history(doc)
    if not events:
        return {"status": "legacy_untracked", "event_count": 0, "events": [], "reasons": []}
    from trading_team.decision_pipeline import canonical_plan_hash
    plan = json.loads((team / "outputs" / doc["date"] / "plan.json").read_text(encoding="utf-8"))
    if plan.get("date") != doc["date"] or canonical_plan_hash(plan) != doc.get("plan_hash"):
        raise ValueError("approval plan_hash is not current")
    validate_decision_facts(doc, plan)
    reasons = []
    projection = doc["approval_projection"]
    if projection["status"] != "complete":
        reasons.append("approval projection incomplete")
    try:
        log = json.loads((team / "loops/approvals.json").read_text(encoding="utf-8"))
        for expected in projected_entries(doc):
            rows = [row for row in log["decisions"] if row.get("date") == expected["date"]
                    and row.get("plan_hash") == expected["plan_hash"] and str(row.get("code")) == expected["code"]]
            if len(rows) != 1 or any(rows[0].get(key) != value for key, value in expected.items()):
                reasons.append("approval log differs from decision events")
                break
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        reasons.append("approval log missing or invalid")
    db_path = team / "team.db"
    receipts = projection.get("destinations", {})
    if db_path.exists():
        try:
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                for event in events:
                    row = conn.execute("SELECT decision_date,code,decision_type,decided_at,source,note,plan_hash,event_id,event_hash "
                                       "FROM approvals WHERE decision_date=? AND event_id=?", (doc["date"], event["event_id"])).fetchone()
                    if row != database_event(event):
                        reasons.append("approval database event missing or conflicting")
                        break
                row = conn.execute("SELECT pm_status,plan_hash FROM decisions WHERE date=?", (doc["date"],)).fetchone()
                expected = (doc.get("pm_status"), doc.get("plan_hash"))
                plans = conn.execute("SELECT code,approval,approved_plan_hash,executable FROM plans WHERE decision_date=? ORDER BY code", (doc["date"],)).fetchall()
                expected_plans = sorted((str(p["code"]), p.get("approval", "pending"), p.get("approved_plan_hash"), bool(p.get("executable"))) for p in doc.get("plans", []))
                if row != expected or plans != expected_plans:
                    reasons.append("approval database projection differs from decision")
        except (OSError, sqlite3.Error):
            reasons.append("approval database projection unavailable")
    elif receipts.get("team_db") == "synced":
        reasons.append("approval database projection missing")
    if receipts.get("agent_md") == "synced":
        try:
            text = (team.parent / "AGENT.md").read_text(encoding="utf-8")
            if any(marker(event) not in text for event in events):
                reasons.append("approval human audit marker missing")
        except OSError:
            reasons.append("approval human audit missing")
    return {"status": "pending" if reasons else "complete", "event_count": len(events),
            "events": events, "reasons": reasons}
