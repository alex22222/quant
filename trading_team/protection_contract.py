"""Explicit, versioned standing stops for existing Paper positions only."""
from datetime import date
from decimal import Decimal
import re

from paper.execution_model import PRICE_TICK
from trading_team.order_conditions import condition_errors, finite_number

PROTECTION_POLICY_VERSION = "existing_position_stop.v1"


def protection_contract_errors(plan: dict) -> list[str]:
    if "protection" not in plan:
        return []
    order = plan["protection"]
    if not isinstance(order, dict):
        return ["protection must be an explicit command object"]
    action = order.get("action")
    keys = {"action", "expected_order_hash"}
    if action in ("create", "replace"):
        keys |= {"type", "price", "qty", "valid_until"}
    errors = []
    if action not in ("create", "replace", "cancel") or set(order) != keys:
        errors.append("protection requires exact create, replace or cancel fields")
    expected = order.get("expected_order_hash")
    if ((action == "create" and expected is not None)
            or (action in ("replace", "cancel") and not re.fullmatch(r"[0-9a-f]{64}", str(expected)))):
        errors.append("protection requires the exact prior order hash; create requires null")
    allowed = ("持有", "卖出", "减持") if action == "cancel" else ("持有",)
    if (plan.get("direction") not in allowed or not finite_number(plan.get("position_pct"))
            or plan["position_pct"] != 0 or plan.get("entry") is not None or plan.get("entry_type") is not None):
        errors.append("protection requires an existing holding; only cancel can accompany a DAY exit")
    errors.extend(condition_errors(plan))
    if plan.get("direction") == "持有" and (plan.get("exit") is not None or plan.get("execution_conditions")):
        errors.append("standing protection cannot contain a DAY exit or extra conditions")
    if action in ("create", "replace"):
        price = order.get("price")
        if order.get("type") != "stop" or order.get("qty") != "all_existing":
            errors.append("protection supports stop for all_existing inventory only")
        if not finite_number(price) or price <= 0 or Decimal(str(price)) % PRICE_TICK:
            errors.append("protection price must be positive with a 0.01 tick")
        try:
            if date.fromisoformat(order.get("valid_until")).isoformat() != order["valid_until"]:
                raise ValueError()
        except (TypeError, ValueError):
            errors.append("protection valid_until must be an ISO date")
    return errors
