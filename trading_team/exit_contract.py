"""Explicit DAY exits. Absence, watch, rejection and risk-reference stops are not orders."""
from dataclasses import replace
from decimal import Decimal

from paper.execution_model import Bar, FillResult, PRICE_TICK, simulate_fill, simulate_limit_fill
from trading_team.order_conditions import condition_errors, evaluate_conditions, finite_number
from trading_team.paper_inventory import available_quantity

EXIT_CONTRACT_VERSION = "a_share_exit.v1"
PORTFOLIO_POLICY_VERSION = "explicit_intent.v1"


def exit_contract_errors(plan: dict) -> list[str]:
    errors = []
    direction = plan.get("direction")
    if direction not in ("卖出", "减持"):
        errors.append("exit requires explicit sell or reduce direction")
    if not finite_number(plan.get("position_pct")) or plan["position_pct"] != 0:
        errors.append("exit position_pct must be zero; use exit.qty for quantity")
    if plan.get("entry") is not None or plan.get("entry_type") is not None:
        errors.append("exit must not contain a buy entry or entry_type")
    errors.extend(condition_errors(plan))
    order = plan.get("exit")
    if not isinstance(order, dict):
        return errors + ["exit must be an explicit order object"]
    kind = order.get("type")
    keys = {"type", "qty", "time_in_force"} | ({"price"} if kind in ("limit", "stop") else set())
    if set(order) != keys or kind not in ("market", "limit", "stop"):
        errors.append("exit requires exact market, limit or stop fields")
    if order.get("time_in_force") != "DAY":
        errors.append("only DAY exits are supported")
    qty = order.get("qty")
    if direction == "卖出" and qty != "all":
        errors.append("full sell requires qty=all")
    if direction == "减持" and (not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0 or qty % 100):
        errors.append("reduce requires positive integer board-lot qty")
    if kind in ("limit", "stop"):
        price = order.get("price")
        if not finite_number(price) or price <= 0:
            errors.append("exit price must be finite and positive")
        elif Decimal(str(price)) % PRICE_TICK:
            errors.append("exit price must use a 0.01 price tick")
    return errors


def simulate_exit(plan: dict, position: dict, bar: Bar, execution_day: str,
                  *, slippage_rate: float):
    errors = exit_contract_errors(plan)
    if errors:
        return FillResult(False, "sell", reject_reason="exit contract: " + "; ".join(errors)), None
    order = plan["exit"]
    planned = order.get("price")
    available = position.get("qty", 0)
    if not isinstance(available, int) or isinstance(available, bool) or available <= 0:
        return FillResult(False, "sell", reject_reason="no verified position to sell"), planned
    qty = available if order["qty"] == "all" else order["qty"]
    if plan["direction"] == "减持" and qty >= available:
        return FillResult(False, "sell", qty=qty, reject_reason="reduce qty must be less than existing holding"), planned
    try:
        if qty > available_quantity(position, execution_day):
            raise ValueError("frozen")
    except (TypeError, ValueError, KeyError, AttributeError):
        return FillResult(False, "sell", qty=qty, reject_reason="T+1 quantity frozen or acquisition lots unverified"), planned
    if bar.suspended or bar.volume == 0 or not finite_number(bar.open) or bar.open <= 0:
        return FillResult(False, "sell", qty=qty, reject_reason="suspended or opening observation missing"), planned
    condition = evaluate_conditions(plan, bar, "sell")
    if condition is not None:
        return replace(condition, qty=qty), planned
    if order["type"] == "limit":
        fill = simulate_limit_fill("sell", qty, planned, bar, slippage_rate=slippage_rate)
    elif order["type"] == "stop":
        if not finite_number(bar.low) or bar.low <= 0:
            return FillResult(False, "sell", qty=qty, reject_reason="stop observation missing"), planned
        if bar.low > planned:
            return FillResult(False, "sell", qty=qty, status="unfilled", reject_reason="止损价未触发"), planned
        fill = simulate_fill("sell", qty, replace(bar, open=min(bar.open, planned)),
                             slippage_rate=slippage_rate)
        if fill.filled:
            fill.price_type = "stop_gap_open" if bar.open <= planned else "stop_daily_bar"
    else:
        fill = simulate_fill("sell", qty, bar, slippage_rate=slippage_rate)
    return replace(fill, qty=qty), planned
