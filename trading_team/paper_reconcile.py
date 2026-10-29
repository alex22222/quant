"""Read-only account reconciliation; a matching ledger is not certified execution."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sqlite3

from paper.execution_model import DEFAULT_FEES, EXECUTION_MODEL_VERSION, total_fee
from trading_team.collect import DEFAULT_TRADING_CALENDAR
from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.execute import SLIPPAGE_RATE, _simulate_buy
from trading_team.entry_contract import ENTRY_CONTRACT_VERSION
from trading_team.exit_contract import EXIT_CONTRACT_VERSION, PORTFOLIO_POLICY_VERSION, simulate_exit
from trading_team.execution_bars import SHANGHAI, _bar_from_row, validate_document
from trading_team.market_archive import completed_day
from trading_team.paper_plans import select_paper_plans, select_paper_proposals
from trading_team.paper_protection import replay_protection, verify_protection_sources, verify_protection_sessions
from trading_team.schemas import compute_executable, is_sell
from trading_team.risk_contract import RISK_CONTRACT_VERSION, RISK_POLICY_VERSION, validate_risk_document
from trading_team.paper_inventory import INVENTORY_POLICY_VERSION, inventory_basis

TEAM = Path(__file__).resolve().parent
CENT = Decimal("0.01")


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _number(value) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite account number")
    return number


def _money(value) -> Decimal:
    return _number(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _issue(issues: list, code: str, severity: str = "conflict", **facts) -> None:
    issues.append({"code": code, "severity": severity, **facts})


def _read_facts(conn: sqlite3.Connection) -> dict:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"account", "positions", "pending_orders", "trades"} <= tables:
        raise ValueError("account tables missing")
    facts = {}
    for table in ("account", "positions", "pending_orders", "trades", "order_events", "execution_receipts"):
        cursor = conn.execute(f"SELECT * FROM {table} ORDER BY rowid") if table in tables else None
        facts[table] = ([dict(zip([col[0] for col in cursor.description], row)) for row in cursor]
                        if cursor else [])
    return facts


def _arithmetic(facts: dict, issues: list) -> dict:
    balances = {row["day"]: row for row in facts["account"]}
    initial = balances.get("init")
    if initial is None or _money(initial["cash"]) != _money(initial["equity"]):
        raise ValueError("cash-only initial account missing; cannot reconstruct ledger")
    cash = _money(initial["cash"])
    positions = {}
    acquisition_lots = {}
    days = sorted((set(balances) - {"init"}) | {row["day"] for row in facts["trades"]}
                  | {row["execution_day"] for row in facts["pending_orders"] + facts["execution_receipts"]})
    for day in days:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("noncanonical account date")
        for trade in (row for row in facts["trades"] if row["day"] == day):
            qty, price = _number(trade["qty"]), _number(trade["price"])
            amount, fee = _money(trade["amount"]), _money(trade["fee"])
            ref = {"date": day, "trade_id": trade["id"], "symbol": trade["code"]}
            if qty <= 0 or qty != int(qty) or price <= 0 or amount <= 0 or fee < 0:
                raise ValueError("invalid trade quantity, price, amount or fee")
            if trade["side"] not in ("buy", "sell"):
                raise ValueError("unknown trade side")
            if price != price.quantize(CENT):
                _issue(issues, "price_tick", **ref, actual=float(price), expected="0.01 RMB tick")
            if amount != _money(qty * price):
                _issue(issues, "amount_price", **ref, actual=float(amount), expected=float(_money(qty * price)))
            expected_fee = total_fee(trade["side"], float(amount))
            if fee != _money(expected_fee):
                _issue(issues, "fee_policy", **ref, actual=float(fee), expected=expected_fee)
            signal = trade.get("signal_day")
            if not signal or date.fromisoformat(signal).isoformat() != signal or signal >= day:
                _issue(issues, "signal_causality", **ref, actual=signal, expected=f"before {day}")
            old = positions.get(trade["code"], {"qty": Decimal(0), "cost": Decimal(0), "buy_day": None})
            if trade["side"] == "buy":
                acquisition_lots.setdefault(trade["code"], []).append([day, int(qty)])
                if qty % 100:
                    _issue(issues, "buy_lot", **ref, actual=int(qty), expected="100-share multiple")
                cash -= amount + fee
                positions[trade["code"]] = {"qty": old["qty"] + qty,
                    "cost": old["cost"] + amount + fee, "buy_day": day}
            else:
                lots = acquisition_lots.get(trade["code"], [])
                available = sum(count for acquired, count in lots if acquired < day)
                if qty > old["qty"] or qty > available:
                    _issue(issues, "sell_inventory_t1", **ref, actual=int(qty), expected=str(old))
                remaining = int(qty)
                for lot in lots:
                    used = min(lot[1], remaining) if lot[0] < day else 0
                    lot[1] -= used
                    remaining -= used
                cash += amount - fee
                if old["qty"] > qty:
                    positions[trade["code"]] = {**old, "qty": old["qty"] - qty,
                        "cost": old["cost"] * (old["qty"] - qty) / old["qty"]}
                else:
                    positions.pop(trade["code"], None)
        cash = _money(cash)
        if day not in balances:
            _issue(issues, "cash_ledger", date=day, actual=None, expected=float(cash))
        elif _money(balances[day]["cash"]) != cash:
            _issue(issues, "cash_ledger", date=day, actual=balances[day]["cash"], expected=float(cash))
    actual_positions = {row["code"]: row for row in facts["positions"]}
    for code in sorted(actual_positions.keys() | positions.keys()):
        actual, expected = actual_positions.get(code), positions.get(code)
        matches = actual is not None and expected is not None and (
            _number(actual["qty"]) == expected["qty"] and actual["buy_day"] == expected["buy_day"]
            and abs(_number(actual["avg_cost"]) - expected["cost"] / expected["qty"]) <= Decimal("0.000001"))
        if not matches:
            _issue(issues, "positions_ledger", symbol=code, actual=actual,
                   expected=None if expected is None else {"qty": int(expected["qty"]),
                       "avg_cost": float(expected["cost"] / expected["qty"]), "buy_day": expected["buy_day"]})
    def signature(row, *, order=False):
        return tuple(row.get(key) for key in (
            "execution_day" if order else "day", "signal_day", "code", "side", "qty",
            "fill_price" if order else "price", "fee", "plan_hash", "planned_price", "signal_source"))
    filled = Counter(signature(row, order=True) for row in facts["pending_orders"] if row["status"] == "filled")
    traded = Counter(signature(row) for row in facts["trades"])
    if filled != traded:
        _issue(issues, "order_trade", actual=sum(traded.values()), expected=sum(filled.values()),
               detail="filled orders and trades are not a one-to-one match")
    orders = {row["id"]: row for row in facts["pending_orders"]}
    event_fields = ("signal_day", "execution_day", "code", "side", "qty", "planned_price", "status", "plan_hash", "fee", "fill_price")
    for event in facts["order_events"]:
        order = orders.get(event["order_id"])
        if order is None or any(event[key] != order[key] for key in event_fields):
            _issue(issues, "order_event", date=event["execution_day"], order_id=event["order_id"])
    missing_events = orders.keys() - {row["order_id"] for row in facts["order_events"]}
    if missing_events:
        _issue(issues, "events_missing", "missing", order_ids=sorted(missing_events))
    codes = {issue["code"] for issue in issues}
    return {"cash": "conflict" if "cash_ledger" in codes else "matched",
            "positions": "conflict" if "positions_ledger" in codes else "matched",
            "order_trade": "conflict" if "order_trade" in codes else "matched",
            "reconstructed_cash": float(cash)}


def _opening_inventory(facts: dict, day: str) -> dict:
    positions = {}
    for trade in sorted(facts["trades"], key=lambda row: (row["day"], row["id"])):
        if trade["day"] >= day:
            continue
        code = trade["code"]
        current = positions.get(code, {"qty": 0, "buy_day": None})
        if trade["side"] == "buy":
            positions[code] = {"qty": current["qty"] + trade["qty"], "buy_day": trade["day"]}
        elif current["qty"] > trade["qty"]:
            positions[code] = {**current, "qty": current["qty"] - trade["qty"]}
        else:
            positions.pop(code, None)
    return positions


def _day_evidence(day: str, name: str, facts: dict, team: Path, market_team: Path,
                  calendar: Path, now: datetime, issues: list, protection: dict | None = None) -> dict:
    orders = [row for row in facts["pending_orders"] if row["execution_day"] == day]
    trades = [row for row in facts["trades"] if row["day"] == day]
    result = {"orders": len(orders), "trades": len(trades), "price_replay": "unavailable", "file_hashes": {}}
    policy_drift = []
    selection_changed = False
    opening_positions = None
    receipts = [row for row in facts["execution_receipts"] if row["execution_day"] == day]
    try:
        recorded_inputs = json.loads(receipts[0]["result_json"])["execution_inputs"]
        if not isinstance(recorded_inputs, dict):
            raise ValueError("receipt execution inputs are not an object")
        recorded_selection = recorded_inputs.get("portfolio_policy")
        recorded_risk_policy = recorded_inputs.get("risk_policy")
    except (IndexError, KeyError, TypeError, ValueError):
        recorded_selection = None
        recorded_risk_policy = None
    def read(path):
        content = path.read_bytes()
        result["file_hashes"][str(path)] = hashlib.sha256(content).hexdigest()
        return json.loads(content)
    try:
        plan = read(team / "outputs" / day / "plan.json")
        decision = read(team / "decisions" / day / "paper_decision.json")
        plan_hash = canonical_plan_hash(plan)
        result["plan_hash"] = plan_hash
        risk_verified = validate_risk_document(plan)
        result["risk_review_state"] = "validated_contract" if risk_verified else "legacy_missing"
        if not risk_verified:
            _issue(issues, "risk_review_evidence", "missing", date=day,
                   detail="legacy risk reasons cannot authorize automatic Paper or certify gate-only restoration")
        selected = select_paper_plans(plan)
        chosen = decision["portfolios"][name]["plans"]
        candidates = {row["code"]: row for row in chosen}
        if ([row["code"] for row in selected] != [row["code"] for row in chosen]
                and recorded_selection != PORTFOLIO_POLICY_VERSION):
            legacy = select_paper_proposals(plan)
            if [row["code"] for row in legacy] == [row["code"] for row in chosen]:
                selected = legacy
                selection_changed = True
        selected_by_code = {row["code"]: row for row in selected}
        market_ok = bool(plan.get("market_ok")) if name == "gate" else True
        quarantined = not risk_verified and decision.get("risk_policy") == RISK_POLICY_VERSION
        if (plan["date"] != day or decision["date"] != day or decision["plan_hash"] != plan_hash
                or decision["data_asof"] != plan["data_asof"] or decision["eligible_execution_day"] != day
                or not plan.get("data_asof") or plan["data_asof"] >= day
                or list(candidates) != list(selected_by_code) or len(chosen) != len(selected)
                or any(any(row.get(key) != value for key, value in selected_by_code[code].items())
                       or row.get("approved_plan_hash") != (None if quarantined else plan_hash)
                       or (quarantined and (row.get("approval") != "pending"
                                            or row.get("approval_source") != "risk_evidence_missing"
                                            or row.get("executable") is not False))
                       for code, row in candidates.items())
                or any(row["plan_hash"] != plan_hash for row in orders + trades)):
            raise ValueError("plan, decision or recorded version disagrees")
        result["plan_binding"] = "matched"
        if selection_changed:
            _issue(issues, "decision_selection_changed", "missing", date=day,
                   detail="historical buy-only decision matches its plan; current explicit exits were not materialized")
        from trading_team.paper_pipeline import paper_executable
        policy_drift = [code for code, row in candidates.items()
                        if row.get("executable") != (
                            paper_executable(row, market_ok, plan_hash, risk_verified)[0]
                            if recorded_risk_policy == RISK_POLICY_VERSION
                            else compute_executable(row, market_ok, plan_hash)[0])]
        materialized = decision.get("materialized_at")
        result["materialized_at"] = materialized
        try:
            published = datetime.fromisoformat(materialized)
            cutoff = datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=SHANGHAI)
            before_open = published.tzinfo is not None and published.astimezone(SHANGHAI) < cutoff
        except (TypeError, ValueError):
            before_open = False
        if not before_open:
            _issue(issues, "publication_timing", "missing", date=day, actual=materialized,
                   detail="current materialization cannot establish pre-execution publication")
        if any(row.get("condition") or row.get("invalid_if") for row in selected):
            _issue(issues, "unmodeled_condition", "missing", date=day,
                   detail="free-text entry conditions are not verified by the daily-bar matcher")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if "risk contract:" in str(exc):
            _issue(issues, "risk_review_conflict", date=day, detail=str(exc))
        _issue(issues, "plan_binding", "missing" if isinstance(exc, FileNotFoundError) else "conflict",
               date=day, detail=str(exc))
        result["plan_binding"] = "unavailable"
        candidates = None
    if not receipts:
        _issue(issues, "receipt_missing", "missing", date=day)
        result["receipt"] = "missing"
    else:
        try:
            from trading_team.paper_pipeline import _execution_inputs_hash
            receipt = receipts[0]
            if receipt["evidence_kind"] != "execution":
                raise KeyError("legacy receipt does not certify execution inputs")
            previous = json.loads(receipt["result_json"])
            inputs = previous["execution_inputs"]
            if not isinstance(inputs, dict):
                raise ValueError("receipt execution inputs are not an object")
            if (_execution_inputs_hash(inputs) != previous["execution_inputs_hash"]
                    or previous["orders"] != len(orders) or previous["fills"] != len(trades)
                    or receipt["plan_hash"] != result.get("plan_hash")):
                raise ValueError("receipt input hash, version or counts disagree")
            result["recorded_model"] = inputs.get("model")
            result["recorded_entry_contract"] = inputs.get("entry_contract")
            result["recorded_exit_contract"] = inputs.get("exit_contract")
            result["recorded_portfolio_policy"] = inputs.get("portfolio_policy")
            result["recorded_risk_policy"] = inputs.get("risk_policy")
            result["recorded_inventory_policy"] = inputs.get("inventory_policy")
            recorded_policy = {key: inputs.get(key) for key in (
                "model", "fees", "slippage_rate", "entry_contract", "exit_contract", "portfolio_policy", "risk_policy", "inventory_policy")}
            current_policy = {"model": EXECUTION_MODEL_VERSION, "fees": asdict(DEFAULT_FEES),
                              "slippage_rate": SLIPPAGE_RATE, "entry_contract": ENTRY_CONTRACT_VERSION,
                              "exit_contract": EXIT_CONTRACT_VERSION, "portfolio_policy": PORTFOLIO_POLICY_VERSION,
                              "risk_policy": RISK_POLICY_VERSION, "inventory_policy": INVENTORY_POLICY_VERSION}
            if recorded_policy != current_policy:
                _issue(issues, "execution_policy_changed", "missing", date=day,
                       actual=recorded_policy, expected=current_policy,
                       detail="current-model comparison does not certify the recorded execution policy")
            result["receipt"] = "internally_matched"
            result["receipt_created_at"] = receipt["created_at"]
            opening_positions = previous.get("sizing_basis", {}).get("positions")
            if inputs.get("inventory_policy") == INVENTORY_POLICY_VERSION:
                expected_inventory = inventory_basis([row for row in facts["trades"] if row["day"] < day], day)
                if previous.get("inventory_basis") != expected_inventory:
                    _issue(issues, "inventory_basis_conflict", date=day,
                           detail="receipt opening lots or trade fingerprint disagree with ledger")
                    result["inventory_basis"] = "conflict"
                else:
                    result["inventory_basis"] = "matched_ledger"
                closing = inventory_basis([row for row in facts["trades"] if row["day"] <= day], day)
                if previous.get("inventory_closing") != closing:
                    _issue(issues, "inventory_closing_conflict", date=day,
                           detail="receipt closing lots or trade fingerprint disagree with ledger")
            else:
                _issue(issues, "inventory_basis_evidence", "missing", date=day,
                       detail="historical receipt lacks acquisition-lot policy; not recertified")
        except ValueError as exc:
            _issue(issues, "receipt_conflict", date=day, detail=str(exc))
            result["receipt"] = "conflict"
        except (KeyError, TypeError) as exc:
            _issue(issues, "receipt_evidence", "missing", date=day, detail=str(exc))
            result["receipt"] = "unverified"
    if policy_drift:
        current_contract = all(
            result.get("recorded_exit_contract") == EXIT_CONTRACT_VERSION
            if is_sell(candidates[code].get("direction"))
            else result.get("recorded_entry_contract") == ENTRY_CONTRACT_VERSION
            for code in policy_drift)
        _issue(issues, "decision_policy_conflict" if current_contract else "decision_policy_changed",
               "conflict" if current_contract else "missing", date=day, symbols=policy_drift,
               detail="recorded eligibility differs from the current order contract; original plan binding retained")
    if candidates is not None and protection is not None:
        for proposal in protection.get("days", {}).get(day, {}).get("exits", []):
            candidates[proposal["code"]] = proposal
    try:
        if completed_day(day, calendar, now) != day:
            raise ValueError("not a completed exchange session")
        document = validate_document(day, read(market_team / "replay_data" / f"{day}.json"))
        bars = {code: _bar_from_row(code, row, day) for code, row in document["bars"].items()}
        if not {row["code"] for row in orders} <= bars.keys():
            raise ValueError("order symbols missing from raw market evidence")
        result["market_hash"] = document["payload_hash"]
        if candidates is not None and not policy_drift and not selection_changed:
            mismatches = []
            skipped = False
            ledger_positions = _opening_inventory(facts, day)
            for row in orders:
                if row["side"] == "buy":
                    proposal = candidates[row["code"]]
                    if not proposal["executable"]:
                        expected = {"status": "blocked", "fill_price": None, "fee": 0.0}
                    else:
                        fill, _, reason = _simulate_buy(proposal, row["qty"], bars[row["code"]])
                        expected = ({"status": fill.status, "fill_price": fill.price or None, "fee": fill.fee} if fill
                                    else {"status": "unfilled" if "未触发" in reason else "rejected", "fill_price": None, "fee": 0.0})
                elif row["side"] == "sell":
                    proposal = candidates.get(row["code"])
                    if proposal is None or not is_sell(proposal.get("direction")):
                        severity = ("conflict" if result.get("recorded_portfolio_policy") == PORTFOLIO_POLICY_VERSION
                                    else "missing")
                        _issue(issues, "exit_intent_missing", severity, date=day, symbol=row["code"])
                        skipped = True
                        continue
                    if not proposal["executable"]:
                        expected = {"status": "blocked", "fill_price": None, "fee": 0.0,
                                    "qty": 0, "planned_price": None}
                    else:
                        position = opening_positions.get(row["code"], {}) if isinstance(opening_positions, dict) else None
                        ledger_position = ledger_positions.get(row["code"], {})
                        if (position is None or not isinstance(position, dict)
                                or any(key not in position for key in ledger_position)):
                            _issue(issues, "exit_inventory_evidence", "missing", date=day, symbol=row["code"])
                            result["exit_inventory"] = "unavailable"
                            skipped = True
                            continue
                        if {key: position.get(key) for key in ("qty", "buy_day")} != {
                                key: ledger_position.get(key) for key in ("qty", "buy_day")}:
                            _issue(issues, "exit_inventory_conflict", date=day, symbol=row["code"],
                                   actual=position, expected=ledger_position)
                            result["exit_inventory"] = "conflict"
                            skipped = True
                            continue
                        if result.get("recorded_inventory_policy") == INVENTORY_POLICY_VERSION:
                            expected_lots = inventory_basis(
                                [trade for trade in facts["trades"] if trade["day"] < day], day)["positions"].get(row["code"], {}).get("lots")
                            if position.get("lots") != expected_lots or result.get("inventory_basis") != "matched_ledger":
                                _issue(issues, "exit_inventory_conflict", date=day, symbol=row["code"],
                                       detail="opening acquisition lots disagree with trade ledger")
                                result["exit_inventory"] = "conflict"
                                skipped = True
                                continue
                        result.setdefault("exit_inventory", "matched_ledger")
                        fill, planned = simulate_exit(proposal, position, bars[row["code"]], day,
                                                      slippage_rate=SLIPPAGE_RATE)
                        expected = {"status": fill.status, "fill_price": fill.price or None, "fee": fill.fee,
                                    "qty": fill.qty, "planned_price": planned}
                else:
                    raise ValueError("unknown order side")
                actual = {key: row[key] for key in expected}
                if expected != actual:
                    mismatches.append(row["id"])
                    _issue(issues, "fill_replay", date=day, order_id=row["id"], symbol=row["code"], actual=actual, expected=expected)
            result["price_replay"] = "conflict" if mismatches else "unavailable" if skipped else "matched_recorded_quantity"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _issue(issues, "market_evidence", "missing", date=day, detail=str(exc))
    return result


def reconcile_account(path: Path, name: str, *, team: Path, market_team: Path,
                      calendar_path: Path, now: datetime) -> dict:
    result = {"status": "missing", "execution_certified": False, "checks": {}, "days": {}, "issues": []}
    if not path.exists():
        return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("BEGIN")
            facts = _read_facts(conn)
        result["facts_hash"] = _hash(facts)
        for table, key, expected in (("trades", "signal_source", f"llm_{name}"),
                                     ("pending_orders", "signal_source", f"llm_{name}"),
                                     ("order_events", "portfolio", name)):
            for row in facts[table]:
                if row.get(key) != expected:
                    _issue(result["issues"], "account_ownership", table=table, row_id=row["id"],
                           date=row.get("execution_day", row.get("day")), symbol=row["code"],
                           actual=row.get(key), expected=expected)
        result["checks"] = _arithmetic(facts, result["issues"])
        protection = None
        try:
            from trading_team.collect import load_trading_days
            protection = replay_protection(facts["execution_receipts"], facts["trades"], facts["pending_orders"], facts["account"])
            verify_protection_sources(protection, team, name)
            verify_protection_sessions(protection, load_trading_days(calendar_path))
            result["checks"]["protection"] = "matched_sources" if protection["days"] else "not_recorded"
        except (OSError, ValueError, TypeError, KeyError) as exc:
            protection = None
            result["checks"]["protection"] = "unverified"
            _issue(result["issues"], "protection_evidence", detail=str(exc))
        days = sorted({row["day"] for row in facts["account"] if row["day"] != "init"}
                      | {row["day"] for row in facts["trades"]}
                      | {row["execution_day"] for row in facts["pending_orders"] + facts["execution_receipts"]})
        for day in days:
            result["days"][day] = _day_evidence(day, name, facts, team, market_team, calendar_path, now, result["issues"], protection)
        severities = Counter(issue["severity"] for issue in result["issues"])
        result.update(status="conflict" if severities["conflict"] else "incomplete" if severities["missing"] else "checked",
                      conflict_count=severities["conflict"], missing_count=severities["missing"],
                      recorded_trade_count=len(facts["trades"]), forward_performance="unverified")
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, sqlite3.Error) as exc:
        result.update(status="invalid", error=str(exc))
    return result


def reconcile_accounts(*, team: Path = TEAM, market_team: Path | None = None,
                       calendar_path: Path = DEFAULT_TRADING_CALENDAR, now: datetime | None = None) -> dict:
    from trading_team.paper_pipeline import paper_db_paths
    current = now or datetime.now(SHANGHAI)
    current = current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    return {"schema": "paper_reconciliation.v1", "observed_at": current.isoformat(),
            "model": EXECUTION_MODEL_VERSION, "fee_policy": asdict(DEFAULT_FEES), "slippage_rate": SLIPPAGE_RATE,
            "entry_contract": ENTRY_CONTRACT_VERSION,
            "exit_contract": EXIT_CONTRACT_VERSION, "portfolio_policy": PORTFOLIO_POLICY_VERSION,
            "risk_contract": RISK_CONTRACT_VERSION, "risk_policy": RISK_POLICY_VERSION,
            "inventory_policy": INVENTORY_POLICY_VERSION,
            "scope": "historical_account_checks_not_execution_certification",
            "limitations": ["Cash-only initial account; no external transfers or corporate actions",
                            "Price replay uses recorded quantities, not independently reconstructed target sizing",
                            "Current model and fee comparison does not prove the historical runtime policy",
                            "Exit replay requires explicit intent and opening inventory matching the trade ledger",
                            "No certification of forward publication, free-text conditions or intraday queue fills"],
            "portfolios": {name: reconcile_account(path, name, team=team, market_team=market_team or team,
                            calendar_path=calendar_path, now=current) for name, path in paper_db_paths(team).items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", type=Path, default=TEAM)
    parser.add_argument("--market-team", type=Path)
    args = parser.parse_args()
    result = reconcile_accounts(team=args.team, market_team=args.market_team)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    states = {item["status"] for item in result["portfolios"].values()}
    if states & {"conflict", "invalid"}:
        raise SystemExit(1)
    if states & {"incomplete", "missing"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
