# -*- coding: utf-8 -*-
"""Materialize versioned decisions from LLM plan artifacts."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

try:
    from trading_team.schemas import SchemaError, compute_executable, parse_entry, parse_pct
    from trading_team.state_lock import atomic_write_json, state_lock
    from trading_team.approval_audit import validate_history
except ImportError:
    from schemas import SchemaError, compute_executable, parse_entry, parse_pct
    from state_lock import atomic_write_json, state_lock
    from approval_audit import validate_history

TEAM = Path(__file__).resolve().parent
APPROVAL_FIELDS = (
    "approval", "approval_note", "approved_at", "approved_plan_hash",
    "executable", "blocked_reason",
    "approval_event_id",
)


def _normalized_proposal(proposal: dict) -> dict:
    result = deepcopy(proposal)
    result["code"] = str(result.get("code", ""))
    try:
        result["position_pct"] = parse_pct(result.get("position_pct"))
    except SchemaError:
        pass
    try:
        result["entry"] = parse_entry(result.get("entry"))
    except SchemaError:
        pass
    for field in APPROVAL_FIELDS:
        result.pop(field, None)
    return result


def normalized_plan_document(plan_doc: dict) -> dict:
    """Return only proposal facts that require a new approval when changed."""
    result = {
        "date": plan_doc.get("date"),
        "data_asof": plan_doc.get("data_asof"),
        "market_ok": bool(plan_doc.get("market_ok", False)),
        "proposals": [
            _normalized_proposal(p) for p in plan_doc.get("proposals", [])
        ],
    }
    if "paper_proposals" in plan_doc:
        result["paper_proposals"] = [
            _normalized_proposal(p) for p in plan_doc.get("paper_proposals", [])
        ]
    for key in ("risk_contract", "risk_reviews", "trader_proposals"):
        if key in plan_doc:
            result[key] = deepcopy(plan_doc[key])
    return result


def canonical_plan_hash(plan_doc: dict) -> str:
    payload = json.dumps(
        normalized_plan_document(plan_doc), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def materialize_decision(day: str, team: Path = TEAM) -> dict:
    """Build decision.json; approvals survive only for the identical plan hash."""
    from pipeline.policy import require
    require("llm_plan")
    with state_lock(team):
        return _materialize_decision(day, team)


def _materialize_decision(day: str, team: Path) -> dict:
    plan_path = team / "outputs" / day / "plan.json"
    if not plan_path.exists():
        raise FileNotFoundError(f"计划文件不存在: {plan_path}")
    plan_doc = json.loads(plan_path.read_text(encoding="utf-8"))
    from trading_team.risk_contract import validate_risk_document
    validate_risk_document(plan_doc)
    if str(plan_doc.get("date")) != day:
        raise ValueError(f"plan.json date={plan_doc.get('date')} 与目录 {day} 不一致")

    plan_hash = canonical_plan_hash(plan_doc)
    decision_path = team / "decisions" / day / "decision.json"
    previous = None
    if decision_path.exists():
        previous = json.loads(decision_path.read_text(encoding="utf-8"))
        validate_history(previous)
    same_version = bool(previous and previous.get("plan_hash") == plan_hash)
    old_by_code = {
        str(p.get("code")): p for p in (previous or {}).get("plans", [])
    }

    plans = []
    for proposal in normalized_plan_document(plan_doc)["proposals"]:
        current = deepcopy(proposal)
        old = old_by_code.get(current["code"])
        if (same_version and old
                and old.get("approved_plan_hash") == plan_hash
                and old.get("approval") in ("approved", "rejected", "reduced")):
            for field in APPROVAL_FIELDS:
                if field in old:
                    current[field] = old[field]
            current["executable"], current["blocked_reason"] = compute_executable(
                current, bool(plan_doc.get("market_ok", False)), plan_hash,
            )
        else:
            current.update({
                "approval": "pending",
                "approval_note": "",
                "executable": False,
                "blocked_reason": "approval=pending",
            })
        plans.append(current)

    states = [p["approval"] for p in plans]
    if plans and all(s == "rejected" for s in states):
        pm_status = "rejected"
    elif plans and all(s in ("approved", "rejected", "reduced") for s in states):
        pm_status = "approved"
    elif any(s in ("approved", "rejected", "reduced") for s in states):
        pm_status = "partial"
    else:
        pm_status = "pending"
    decision = {
        "date": day,
        "data_asof": plan_doc.get("data_asof"),
        "plan_hash": plan_hash,
        "source_plan": f"outputs/{day}/plan.json",
        "market_ok": bool(plan_doc.get("market_ok", False)),
        "pm_status": pm_status,
        "materialized_at": datetime.now().isoformat(timespec="seconds"),
        "plans": plans,
    }
    if previous and previous.get("approval_events"):
        decision["approval_events"] = deepcopy(previous["approval_events"])
        decision["approval_projection"] = deepcopy(previous["approval_projection"])
        if not same_version:
            decision["approval_projection"]["status"] = "pending"
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(decision_path, decision)
    return decision
