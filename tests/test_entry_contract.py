import json
import sqlite3

import pytest

from paper.execution_model import Bar
from trading_team.counterfactual import build_counterfactual
from trading_team.execute import _simulate_buy
from trading_team.paper_pipeline import _db, _execute_portfolio
from trading_team.schemas import SchemaError, compute_executable, parse_entry, validate_plan


def proposal(**changes):
    return {
        "code": "600036", "direction": "条件买入", "position_pct": 0.1,
        "entry": [10.5], "entry_type": "limit", "approval": "approved",
        "approved_plan_hash": "v1", **changes,
    }


def bar(**changes):
    return Bar(**{"open": 10.3, "high": 10.8, "low": 10.1, "close": 10.4,
                  "prev_close": 10.0, **changes})


@pytest.mark.parametrize("changes", [
    {"entry_type": "limit_pullback"}, {"entry_type": "breakout_confirm"},
    {"entry_type": None}, {"entry": [10.5, 10.2]}, {"entry": [True]},
    {"entry": [float("nan")]}, {"entry": [float("inf")]}, {"entry": [0]},
    {"entry": [10.501]}, {"entry": ["10.5"]},
    {"condition": "高开超过3%不追"}, {"invalid_if": "跌破均线失效"},
    {"direction": "考虑买入"}, {"execution_conditions": "open > 10"},
    {"execution_conditions": [{"field": "close", "op": "gte", "value": 10}]},
    {"execution_conditions": [{"field": "open_price", "op": "gt", "value": 10}]},
    {"execution_conditions": [{"field": "open_price", "op": "gte", "value": True}]},
    {"execution_conditions": [{"field": "open_price", "op": "gte", "value": 0}]},
    {"execution_conditions": [{"field": "open_gap_pct", "op": "lte", "value": "3%"}]},
    {"execution_conditions": [{"field": "open_gap_pct", "op": "lte", "value": float("nan")}]},
    {"execution_conditions": [{"field": "open_gap_pct", "op": "lte", "value": .03,
                               "note": "also wait for news"}]},
])
def test_unsupported_contract_never_becomes_executable(changes):
    plan = proposal(**changes)
    executable, reason = compute_executable(plan, True, "v1")
    assert not executable
    assert "entry contract" in reason
    assert validate_plan(plan, strict=True)
    fill, _, _ = _simulate_buy(plan, 100, bar())
    assert fill is not None and not fill.filled and fill.status == "rejected"


@pytest.mark.parametrize("value", [True, [True], [float("nan")], [float("inf")], [-1], [0]])
def test_normalization_cannot_launder_invalid_entry(value):
    with pytest.raises(SchemaError):
        parse_entry(value)


@pytest.mark.parametrize("entry_type", ["limit", "breakout"])
@pytest.mark.parametrize("opening,status", [(10.3, "filled"), (10.31, "unfilled")])
def test_open_gap_guard_inclusive_decimal_boundary_and_shared_replay(entry_type, opening, status):
    plan = proposal(entry_type=entry_type, execution_conditions=[
        {"field": "open_gap_pct", "op": "lte", "value": .03},
        {"field": "reference_price", "op": "gte", "value": 10},
        {"field": "open_price", "op": "gte", "value": 10.3},
    ])
    fill, planned, reason = _simulate_buy(plan, 900, bar(open=opening))
    assert planned == 10.5 and not reason
    assert fill.status == status
    report = build_counterfactual({"market_ok": True, "paper_proposals": [plan]},
                                  {"600036": bar(open=opening)})
    for portfolio in ("gate", "no_gate"):
        order = report["portfolios"][portfolio]["orders"][0]
        assert order["status"] == status
        if status == "filled":
            assert order["fill_price"] == fill.price
            assert order["fee"] == fill.fee
        else:
            assert "execution condition" in order["reason"]
            assert fill.fee == 0


@pytest.mark.parametrize("changes", [{"open": None}, {"prev_close": None}, {"prev_close": float("nan")}])
def test_guard_missing_observation_is_rejected_not_assumed(changes):
    plan = proposal(execution_conditions=[{"field": "open_gap_pct", "op": "lte", "value": .03}])
    fill, _, _ = _simulate_buy(plan, 100, bar(**changes))
    assert fill.status == "rejected" and "observation" in fill.reject_reason


def test_no_gate_does_not_bypass_structural_contract():
    plan = proposal(condition="高开超过3%不追")
    report = build_counterfactual({"market_ok": False, "paper_proposals": [plan]}, {"600036": bar()})
    for portfolio in ("gate", "no_gate"):
        order = report["portfolios"][portfolio]["orders"][0]
        assert order["status"] == "blocked" and "entry contract" in order["reason"]


@pytest.mark.parametrize("claimed_executable", [False, True])
def test_blocked_buy_preserves_selected_holding_even_with_stale_executable(tmp_path, claimed_executable):
    path = tmp_path / "paper.db"
    with _db(path) as conn:
        conn.execute("INSERT INTO positions VALUES ('600036',100,10,'2026-09-18')")
        conn.execute("INSERT INTO trades(day,code,side,qty) VALUES ('2026-09-18','600036','buy',100)")
    plan = proposal(condition="高开超过3%不追", executable=claimed_executable, blocked_reason="unsupported")
    decision = {"plan_hash": "v1", "data_asof": "2026-09-18",
                "eligible_execution_day": "2026-09-21",
                "portfolios": {"no_gate": {"market_ok": True, "plans": [plan]}}}
    result = _execute_portfolio(decision, "no_gate", path, {"600036": bar()}, True)
    assert result["fills"] == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT qty FROM positions WHERE code='600036'").fetchone() == (100,)
        assert conn.execute("SELECT side,status FROM pending_orders").fetchall() == [("buy", "blocked")]


def test_entry_contract_version_is_bound_to_settlement(tmp_path, monkeypatch):
    from trading_team import paper_pipeline

    path = tmp_path / "paper.db"
    decision = {"plan_hash": "v1", "data_asof": "2026-09-18", "eligible_execution_day": "2026-09-21",
                "portfolios": {"gate": {"plans": []}}}
    _execute_portfolio(decision, "gate", path, {}, True)
    with sqlite3.connect(path) as conn:
        inputs = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])["execution_inputs"]
    assert inputs["entry_contract"] == "a_share_entry.v1"
    monkeypatch.setattr(paper_pipeline, "ENTRY_CONTRACT_VERSION", "changed")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="execution evidence changed"):
        _execute_portfolio(decision, "gate", path, {}, True)
    assert path.read_bytes() == before


def test_breakout_affordability_retry_retains_breakout_semantics():
    plan = proposal(entry_type="breakout", entry=[1])
    report = build_counterfactual({"market_ok": True, "paper_proposals": [plan]},
                                  {"600036": bar(open=20, high=21, low=19, close=20, prev_close=20)},
                                  initial_cash=10_000)
    order = report["portfolios"]["no_gate"]["orders"][0]
    assert (order["status"], order["qty"], order["fill_price"]) == ("filled", 400, 20.04)


@pytest.mark.parametrize("opening,status", [(10.3, "filled"), (10.31, "unfilled")])
def test_guard_result_is_persisted_in_account_order_and_receipt(tmp_path, opening, status):
    path = tmp_path / "paper.db"
    plan = proposal(executable=True, execution_conditions=[
        {"field": "open_gap_pct", "op": "lte", "value": .03},
    ])
    decision = {"plan_hash": "v1", "data_asof": "2026-09-18", "eligible_execution_day": "2026-09-21",
                "portfolios": {"no_gate": {"plans": [plan]}}}
    _execute_portfolio(decision, "no_gate", path, {"600036": bar(open=opening)}, True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM pending_orders").fetchone() == (status,)
        assert conn.execute("SELECT status FROM order_events").fetchone() == (status,)
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == int(status == "filled")
        receipt = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
        assert receipt["execution_inputs"]["bars"]["600036"]["open"] == opening
        if status == "unfilled":
            assert conn.execute("SELECT fee,fill_price FROM pending_orders").fetchone() == (0, None)
