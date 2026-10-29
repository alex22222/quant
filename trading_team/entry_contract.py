"""Executable buy intent and pre-open guards shared by every LLM Paper path."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from paper.execution_model import Bar, FillResult, PRICE_TICK, simulate_fill, simulate_limit_fill
from trading_team.order_conditions import condition_errors, evaluate_conditions, finite_number as _number

ENTRY_CONTRACT_VERSION = "a_share_entry.v1"


def entry_contract_errors(plan: dict) -> list[str]:
    errors = []
    if plan.get("direction") not in ("买入", "条件买入", "加仓"):
        errors.append("unsupported buy direction")
    if plan.get("entry_type") not in ("limit", "breakout"):
        errors.append("entry_type must be limit or breakout")
    entries = plan.get("entry")
    if (not isinstance(entries, list) or len(entries) != 1
            or not _number(entries[0]) or entries[0] <= 0):
        errors.append("entry must contain one finite positive numeric price")
    elif Decimal(str(entries[0])) % PRICE_TICK:
        errors.append("entry must use a 0.01 price tick")
    return errors + condition_errors(plan)


def simulate_entry(plan: dict, qty: int, bar: Bar, *, slippage_rate: float):
    errors = entry_contract_errors(plan)
    if errors:
        return FillResult(False, "buy", reject_reason="entry contract: " + "; ".join(errors)), None, ""
    planned = float(plan["entry"][0])
    condition = evaluate_conditions(plan, bar, "buy")
    if condition is not None:
        return condition, planned, ""
    if plan["entry_type"] == "breakout":
        if not _number(bar.high) or not _number(bar.open) or bar.high <= 0 or bar.open <= 0:
            return FillResult(False, "buy", reject_reason="breakout observation missing"), planned, ""
        if bar.high < planned:
            return FillResult(False, "buy", status="unfilled", reject_reason="突破价未触发"), planned, ""
        fill = simulate_fill("buy", qty, replace(bar, open=max(bar.open, planned)),
                             slippage_rate=slippage_rate)
    else:
        fill = simulate_limit_fill("buy", qty, planned, bar, slippage_rate=slippage_rate)
    return fill, planned, ""
