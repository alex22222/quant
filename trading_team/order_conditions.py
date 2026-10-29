"""Shared, point-in-time conditions for buy and sell intents."""
from decimal import Decimal
import math

from paper.execution_model import FillResult


def finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def condition_errors(plan: dict) -> list[str]:
    errors = []
    for key in ("condition", "invalid_if"):
        if plan.get(key) not in (None, ""):
            errors.append(f"{key} contains unmodeled text; use execution_conditions or observe only")
    conditions = plan.get("execution_conditions", [])
    if not isinstance(conditions, list):
        return errors + ["execution_conditions must be an array"]
    for index, condition in enumerate(conditions):
        prefix = f"execution_conditions[{index}]"
        if not isinstance(condition, dict) or set(condition) != {"field", "op", "value"}:
            errors.append(f"{prefix} must have exactly field, op, value")
            continue
        field, op, value = condition["field"], condition["op"], condition["value"]
        if field not in ("open_price", "reference_price", "open_gap_pct"):
            errors.append(f"{prefix} unsupported field")
        if op not in ("gte", "lte"):
            errors.append(f"{prefix} unsupported operator")
        if not finite_number(value) or (field != "open_gap_pct" and value <= 0):
            errors.append(f"{prefix} invalid numeric threshold")
        elif field == "open_gap_pct" and not -1 <= value <= 1:
            errors.append(f"{prefix} gap must be a fraction in [-1, 1]")
    return errors


def evaluate_conditions(plan, bar, side):
    # Only open-time observations are allowed; no intraday sequence is inferred.
    for condition in plan.get("execution_conditions", []):
        field, op = condition["field"], condition["op"]
        required = ((bar.open, bar.prev_close) if field == "open_gap_pct" else
                    (bar.open,) if field == "open_price" else (bar.prev_close,))
        if any(not finite_number(value) or value <= 0 for value in required):
            return FillResult(False, side, reject_reason=f"execution condition: {field} observation missing")
        value = Decimal(str(required[0]))
        threshold = Decimal(str(condition["value"]))
        if field == "open_gap_pct":
            reference = Decimal(str(required[1]))
            value -= reference
            threshold *= reference
        passed = value >= threshold if op == "gte" else value <= threshold
        if not passed:
            return FillResult(False, side, status="unfilled", reject_reason=
                              f"execution condition unmet: {field} {op} {condition['value']}")
    return None
