# -*- coding: utf-8 -*-
"""Deterministic Paper candidate selection, separate from Live approvals."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from trading_team.conclusion_block import extract_block
from trading_team.schemas import SchemaError, is_buy, is_sell, parse_pct
from trading_team.risk_contract import derive_paper_proposals, validate_risk_document
from trading_team.state_lock import atomic_write_json, state_lock

PAPER_SINGLE_POSITION_MAX = 0.10
PAPER_MAX_NEW_BUYS = 3


def select_paper_proposals(plan_doc: dict) -> list[dict]:
    """Select at most three valid buys and cap each target at ten percent."""
    selected = []
    source = plan_doc.get("paper_proposals", plan_doc.get("proposals", []))
    for raw in source:
        try:
            pct = parse_pct(raw.get("position_pct"))
        except SchemaError:
            continue
        if not is_buy(str(raw.get("direction", "")), pct) or pct <= 0:
            continue
        plan = deepcopy(raw)
        plan["code"] = str(plan.get("code", ""))
        plan["position_pct"] = min(pct, PAPER_SINGLE_POSITION_MAX)
        selected.append(plan)
        if len(selected) == PAPER_MAX_NEW_BUYS:
            break
    return selected


def select_paper_plans(plan_doc: dict) -> list[dict]:
    """Buy caps never suppress explicit exits; one daily intent per symbol."""
    validate_risk_document(plan_doc)
    source = plan_doc.get("paper_proposals", plan_doc.get("proposals", []))
    codes = [str(plan.get("code", "")) for plan in source]
    if len(codes) != len(set(codes)):
        raise ValueError("duplicate Paper intent for symbol")
    buys = {plan["code"]: plan for plan in select_paper_proposals(plan_doc)}
    selected = []
    for raw in source:
        code = str(raw.get("code", ""))
        if is_sell(str(raw.get("direction", ""))) or "protection" in raw:
            selected.append(dict(deepcopy(raw), code=code))
        elif code in buys:
            selected.append(buys[code])
    return selected


def ensure_paper_proposals(day: str, team: Path) -> dict:
    """Backfill archived plans from the trader's structured report once."""
    from pipeline.policy import require
    require("llm_plan")
    with state_lock(team):
        return _ensure_paper_proposals(day, team)


def _ensure_paper_proposals(day: str, team: Path) -> dict:
    path = team / "outputs" / day / "plan.json"
    plan_doc = json.loads(path.read_text(encoding="utf-8"))
    validate_risk_document(plan_doc)
    if "paper_proposals" in plan_doc:
        return plan_doc
    trader_path = team / "outputs" / day / "06_trader.md"
    trader = []
    if trader_path.exists():
        block = extract_block(trader_path.read_text(encoding="utf-8"), "trader")
        if block and isinstance(block.get("proposals"), list):
            trader = block["proposals"]
    final = plan_doc.get("proposals", [])
    plan_doc["paper_proposals"] = (
        derive_paper_proposals(trader, final) if trader else deepcopy(final)
    )
    atomic_write_json(path, plan_doc)
    return plan_doc
