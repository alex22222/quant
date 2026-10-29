"""Standing stops reconstructed from receipts, never a second mutable order store."""
from copy import deepcopy
from dataclasses import asdict
from datetime import date
import hashlib
import json

from paper.execution_model import Bar
from trading_team.exit_contract import simulate_exit
from trading_team.paper_inventory import attach_inventory, inventory_basis, read_trades
from trading_team.protection_contract import PROTECTION_POLICY_VERSION, protection_contract_errors
from trading_team.order_conditions import finite_number


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def prepare_protection(plans, positions, opening, day, plan_hash, bars, *, slippage_rate):
    if not isinstance(plans, list) or any(not isinstance(plan, dict) for plan in plans):
        raise ValueError("protection plans must be an array of objects")
    state, events, exits = deepcopy(opening), [], []
    for code, guard in list(state.items()):
        if day > guard["valid_until"]:
            events.append({"code": code, "order_hash": guard["order_hash"], "status": "expired"})
            del state[code]
    for plan in plans:
        if "protection" not in plan:
            continue
        errors = protection_contract_errors(plan)
        if errors:
            raise ValueError("protection contract: " + "; ".join(errors))
        code, command = plan["code"], plan["protection"]
        action, current = command["action"], state.get(code)
        event = {"code": code, "action": action, "expected_order_hash": command["expected_order_hash"]}
        if (plan.get("executable") is not True or plan.get("approval") != "approved"
                or plan.get("approved_plan_hash") != plan_hash or plan.get("approval_source") != "paper_auto"):
            event["status"] = "not_authorized"
        elif (action == "create" and current) or (action != "create" and (
                not current or current["order_hash"] != command["expected_order_hash"])):
            event["status"] = "stale_version"
        elif action == "cancel":
            del state[code]
            event.update(status="cancelled", order_hash=current["order_hash"])
        elif code not in positions:
            event["status"] = "no_position"
        elif not 0 <= (date.fromisoformat(command["valid_until"]) - date.fromisoformat(day)).days <= 30:
            event["status"] = "invalid_validity"
        else:
            position = positions[code]
            if not position.get("lots"):
                raise ValueError("protection requires verified acquisition lots")
            authority = {"day": day, "plan_hash": plan_hash, "code": code, "command": command}
            guard = {"code": code, "order_hash": _hash(authority), "plan_hash": plan_hash,
                     "created_day": day, "price": command["price"], "valid_until": command["valid_until"],
                     "qty": position["qty"], "lots": deepcopy(position["lots"])}
            state[code] = guard
            event.update(status="created" if action == "create" else "replaced", order_hash=guard["order_hash"])
        events.append(event)
    for code, guard in list(sorted(state.items())):
        position = positions.get(code, {})
        if position.get("qty") != guard["qty"] or position.get("lots") != guard["lots"]:
            raise ValueError("protection inventory changed; reconcile or explicitly cancel/replace")
        if any(p["code"] == code and p.get("direction") != "持有"
               for p in plans):
            raise ValueError("active protection requires explicit cancellation before another symbol trade")
        if code not in bars:
            raise ValueError("protection market evidence missing")
        exit_plan = {"code": code, "direction": "卖出", "position_pct": 0, "entry": None,
                     "exit": {"type": "stop", "price": guard["price"], "qty": "all", "time_in_force": "DAY"},
                     "executable": True}
        fill, _ = simulate_exit(exit_plan, position, bars[code], day, slippage_rate=slippage_rate)
        exits.append(exit_plan)
        events.append({"code": code, "order_hash": guard["order_hash"], "status": "evaluated", "fill": asdict(fill)})
        if fill.filled:
            del state[code]
    # Wall-clock approval timestamps are not command identity or replay inputs.
    stable_plans = [{k: v for k, v in p.items() if k != "approved_at"} for p in plans]
    return {"policy": PROTECTION_POLICY_VERSION, "plans": deepcopy(stable_plans), "opening": deepcopy(opening),
            "closing": state, "events": events, "exits": exits}, list(plans) + exits


def replay_protection(receipts, trades, orders, accounts):
    """Check state transitions and resulting orders against independent quantity facts."""
    state, checked, started = {}, {}, False
    for row in sorted(receipts, key=lambda item: item["execution_day"]):
        day, result = row["execution_day"], json.loads(row["result_json"])
        if not isinstance(result, dict):
            raise ValueError("protection receipt must be an object")
        inputs = result.get("execution_inputs", {})
        if not isinstance(inputs, dict):
            raise ValueError("protection execution inputs must be an object")
        policy = inputs.get("protection_policy")
        if policy is None and not started and "protection" not in result:
            continue
        if policy != PROTECTION_POLICY_VERSION:
            raise ValueError("protection policy missing or changed in receipt sequence")
        started = True
        try:
            if row["evidence_kind"] != "execution" or result.get("execution_inputs_hash") != _hash(inputs):
                raise ValueError("protection receipt input hash or evidence kind changed")
            saved = result["protection"]
            opening = inventory_basis([t for t in trades if t["day"] < day], day)
            if result["inventory_basis"] != opening:
                raise ValueError("protection opening inventory evidence changed")
            bars = {code: Bar(**bar) for code, bar in inputs["bars"].items()}
            verify_protection_reference({"active": state, "days": checked}, bars)
            expected, _ = prepare_protection(saved["plans"], opening["positions"], state, day,
                                             row["plan_hash"], bars, slippage_rate=inputs["slippage_rate"])
            if saved != expected:
                raise ValueError("protection receipt transition changed")
            for event in expected["events"]:
                if event["status"] != "evaluated":
                    continue
                fill, code = event["fill"], event["code"]
                matches = [o for o in orders if o["execution_day"] == day and o["code"] == code and o["side"] == "sell"]
                values = {"qty": fill["qty"], "status": fill["status"], "fill_price": fill["price"] or None,
                          "fee": fill["fee"], "planned_price": next(p["exit"]["price"] for p in expected["exits"] if p["code"] == code)}
                if len(matches) != 1 or any(matches[0][key] != value for key, value in values.items()):
                    raise ValueError("protection order evidence changed")
            state = expected["closing"]
            checked[day] = {"plan_hash": row["plan_hash"], **expected,
                            "closing_marks": {code: bar.close for code, bar in bars.items()}}
        except (KeyError, TypeError) as exc:
            raise ValueError("protection receipt evidence missing") from exc
    for account in accounts:
        if f"protection={PROTECTION_POLICY_VERSION}" in (account.get("note") or "") and account["day"] not in checked:
            raise ValueError("protection settlement receipt missing")
    return {"policy": PROTECTION_POLICY_VERSION, "active": state, "days": checked}


def read_protection(conn):
    def rows(table):
        cursor = conn.execute(f"SELECT * FROM {table}")
        keys = [column[0] for column in cursor.description]
        return [dict(zip(keys, row)) for row in cursor]
    trades, accounts = read_trades(conn), rows("account")
    history = replay_protection(rows("execution_receipts"), trades, rows("pending_orders"), accounts)
    if history["active"]:
        last_day = max(row["day"] for row in accounts if row["day"] != "init")
        basis = inventory_basis(trades, last_day)
        attach_inventory({row["code"]: row for row in rows("positions")}, basis)
        for code, guard in history["active"].items():
            position = basis["positions"].get(code, {})
            if position.get("qty") != guard["qty"] or position.get("lots") != guard["lots"]:
                raise ValueError("protection current inventory changed")
    return history


def verify_protection_sources(history, team, portfolio):
    """A receipt cannot mint a new standing authorization independent of plan/decision."""
    from trading_team.decision_pipeline import canonical_plan_hash
    from trading_team.paper_pipeline import _validate_paper_decision
    for day, evidence in history["days"].items():
        if not any("protection" in p for p in evidence["plans"]):
            continue
        plan = json.loads((team / "outputs" / day / "plan.json").read_text())
        decision = json.loads((team / "decisions" / day / "paper_decision.json").read_text())
        if decision.get("plan_hash") != canonical_plan_hash(plan) or decision["plan_hash"] != evidence["plan_hash"]:
            raise ValueError("protection source plan version changed")
        _validate_paper_decision(decision, plan, day)
        stable = [{k: v for k, v in p.items() if k != "approved_at"}
                  for p in decision["portfolios"][portfolio]["plans"]]
        if stable != evidence["plans"]:
            raise ValueError("protection source decision changed")


def verify_protection_sessions(history, sessions, next_day=None):
    previous, active = None, False
    for day, evidence in sorted(history["days"].items()):
        if active and sorted(s for s in sessions if previous < s <= day) != [day]:
            raise ValueError("protection session gap; replay missing sessions before continuing")
        previous, active = day, bool(evidence["closing"])
    if next_day and active and next_day > previous:
        if sorted(s for s in sessions if previous < s <= next_day) != [next_day]:
            raise ValueError("protection session gap; replay missing sessions before continuing")


def verify_protection_reference(history, bars):
    if not history["active"]:
        return
    marks = history["days"][max(history["days"])]["closing_marks"]
    for code in history["active"]:
        previous = marks.get(code)
        current = bars.get(code)
        if (not finite_number(previous) or previous <= 0 or current is None
                or current.prev_close != previous):
            raise ValueError("protection reference price discontinuity; corporate-action accounting not verified")
