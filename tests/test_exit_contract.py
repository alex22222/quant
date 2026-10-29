import json
import sqlite3
from copy import deepcopy

import pytest

from paper.execution_model import Bar
from trading_team.counterfactual import build_counterfactual
from trading_team.paper_pipeline import _db, _execute_portfolio, materialize_paper_decision
from trading_team.schemas import compute_executable, is_sell, validate_plan
from trading_team.risk_contract import build_reviewed_plan

DAY = "2026-09-21"


def sell_plan(direction="卖出", kind="market", qty="all", price=None, **changes):
    order = {"type": kind, "qty": qty, "time_in_force": "DAY"}
    if price is not None:
        order["price"] = price
    return {"code": "600036", "direction": direction, "position_pct": 0,
            "entry": None, "entry_type": None, "exit": order,
            "approval": "approved", "approved_plan_hash": "v1", "executable": True, **changes}


def bar(**changes):
    return Bar(**{"open": 10, "high": 10.8, "low": 9.4, "close": 10.2, "prev_close": 10,
                  "limit_up": 11, "limit_down": 9, "volume": 100000, **changes})


def decision(plans):
    return {"date": DAY, "data_asof": "2026-09-18", "plan_hash": "v1", "eligible_execution_day": DAY,
            "portfolios": {"no_gate": {"market_ok": True, "plans": plans}}}


def seed(path, qty=500, cash=95000, buy_day="2026-09-18"):
    conn = _db(path)
    with conn:
        conn.execute("UPDATE account SET cash=?,equity=? WHERE day='init'", (cash, cash + qty * 10))
        conn.execute("INSERT INTO positions VALUES ('600036',?,10,?)", (qty, buy_day))
        conn.execute("INSERT INTO trades(day,code,side,qty) VALUES (?,'600036','buy',?)", (buy_day, qty))
    conn.close()


@pytest.mark.parametrize("direction", [None, "持有", "观望", "回避"])
def test_absence_or_research_opinion_never_liquidates_holding(tmp_path, direction):
    path = tmp_path / "paper.db"
    seed(path)
    plans = [] if direction is None else [{"code": "600036", "direction": direction,
        "position_pct": 0, "entry": None, "executable": True}]
    result = _execute_portfolio(decision(plans), "no_gate", path, {"600036": bar()}, True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT qty FROM positions").fetchone() == (500,)
        assert conn.execute("SELECT COUNT(*) FROM pending_orders").fetchone() == (0,)
    assert result["fills"] == 0
    assert not is_sell(direction or "")


@pytest.mark.parametrize("changes", [
    {"exit": None}, {"exit": {"type": "market", "qty": "all"}},
    {"exit": {"type": "limit", "qty": "all", "time_in_force": "DAY"}},
    {"exit": {"type": "stop", "qty": "all", "time_in_force": "GTC", "price": 9.5}},
    {"exit": {"type": "magic", "qty": "all", "time_in_force": "DAY"}},
    {"exit": {"type": "market", "qty": "all", "time_in_force": "DAY", "price": 10}},
    {"exit": {"type": "market", "qty": 100, "time_in_force": "DAY"}},
    {"exit": {"type": "limit", "qty": "all", "time_in_force": "DAY", "price": 9.999}},
    {"exit": {"type": "limit", "qty": "all", "time_in_force": "DAY", "price": True}},
    {"exit": {"type": "stop", "qty": "all", "time_in_force": "DAY", "price": float("nan")}},
    {"condition": "跌破均线再卖"}, {"invalid_if": "等待新闻"}, {"position_pct": .1},
    {"entry": [10]}, {"direction": "回避"},
    {"execution_conditions": [{"field": "close", "op": "lte", "value": 10}]},
])
def test_ambiguous_exit_is_not_executable(changes):
    plan = sell_plan(**changes)
    executable, reason = compute_executable(plan, False, "v1")
    assert not executable and "exit contract" in reason
    assert validate_plan(plan, strict=True)


@pytest.mark.parametrize("qty", [True, 0, -100, 150, "100", "all", 100.5])
def test_reduction_requires_explicit_board_lot_quantity(qty):
    plan = sell_plan(direction="减持", qty=qty)
    assert not compute_executable(plan, True, "v1")[0]


@pytest.mark.parametrize("kind,price,changes,status,fill_price", [
    ("market", None, {}, "filled", 9.98),
    ("limit", 10.5, {}, "filled", 10.5),
    ("limit", 10.9, {}, "unfilled", 0),
    ("stop", 9.6, {}, "filled", 9.58),
    ("stop", 9.3, {}, "unfilled", 0),
    ("stop", 9.6, {"open": 9.5, "low": 9.3}, "filled", 9.48),
    ("market", None, {"open": 9}, "rejected", 0),
    ("stop", 9.6, {"suspended": True}, "rejected", 0),
    ("market", None, {"open": None}, "rejected", 0),
])
def test_shared_exit_prices_and_unfilled_states(kind, price, changes, status, fill_price):
    from trading_team.exit_contract import simulate_exit
    plan = sell_plan(kind=kind, price=price)
    assert compute_executable(plan, False, "v1")[0]
    fill, _ = simulate_exit(plan, {"qty": 500, "buy_day": "2026-09-18"}, bar(**changes), DAY,
                            slippage_rate=.002)
    assert (fill.status, fill.price) == (status, fill_price)
    if status != "filled":
        assert fill.fee == 0 and fill.amount == 0


@pytest.mark.parametrize("buy_day", [None, "2026-09-21", "2026-09-22", "invalid"])
def test_unknown_or_t1_frozen_position_cannot_exit(buy_day):
    from trading_team.exit_contract import simulate_exit
    fill, _ = simulate_exit(sell_plan(), {"qty": 500, "buy_day": buy_day}, bar(), DAY, slippage_rate=.002)
    assert fill.status == "rejected" and fill.fee == 0


@pytest.mark.parametrize("qty", [500, 600])
def test_partial_exit_cannot_silently_liquidate_or_oversell(qty):
    from trading_team.exit_contract import simulate_exit
    fill, _ = simulate_exit(sell_plan(direction="减持", qty=qty),
                            {"qty": 500, "buy_day": "2026-09-18"}, bar(), DAY, slippage_rate=.002)
    assert fill.status == "rejected"


def test_full_exit_can_liquidate_existing_odd_lot():
    from trading_team.exit_contract import simulate_exit
    fill, _ = simulate_exit(sell_plan(), {"qty": 175, "buy_day": "2026-09-18"}, bar(), DAY, slippage_rate=.002)
    assert fill.filled and fill.qty == 175


def test_partial_exit_is_persisted_without_changing_remaining_cost_or_buy_date(tmp_path):
    path = tmp_path / "paper.db"
    seed(path)
    result = _execute_portfolio(decision([sell_plan(direction="减持", qty=200)]),
                                "no_gate", path, {"600036": bar()}, True)
    assert result["fills"] == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT qty,avg_cost,buy_day FROM positions").fetchone() == (300, 10, "2026-09-18")
        assert conn.execute("SELECT side,qty,status FROM pending_orders").fetchone() == ("sell", 200, "filled")
        receipt = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
        assert receipt["execution_inputs"]["exit_contract"] == "a_share_exit.v1"
        assert receipt["execution_inputs"]["portfolio_policy"] == "explicit_intent.v1"
    before = path.read_bytes()
    assert _execute_portfolio(decision([sell_plan(direction="减持", qty=200)]),
                              "no_gate", path, {"600036": bar()}, True)["status"] == "skipped"
    assert before == path.read_bytes()


def test_materialization_preserves_exit_after_three_buy_candidates(tmp_path):
    team = tmp_path / "trading_team"
    out = team / "outputs" / DAY
    out.mkdir(parents=True)
    buys = [{"code": code, "direction": "买入", "position_pct": .2, "entry": [10], "entry_type": "limit"}
            for code in ("601318", "601899", "600900", "600519")]
    exit_plan = sell_plan()
    for key in ("approval", "approved_plan_hash", "executable"):
        exit_plan.pop(key)
    candidates = buys + [exit_plan]
    plan = build_reviewed_plan(DAY, "2026-09-18", False, candidates,
                               [{"code": p["code"], "decision": "通过", "reason_codes": []} for p in candidates])
    (out / "plan.json").write_text(json.dumps(plan))
    result = materialize_paper_decision(DAY, team)
    for name in ("gate", "no_gate"):
        plans = result["portfolios"][name]["plans"]
        assert len(plans) == 4
        selected = next(p for p in plans if p["code"] == "600036")
        assert selected["executable"] and selected["exit"] == exit_plan["exit"]
        assert selected["approved_plan_hash"] == result["plan_hash"]


def test_counterfactual_exit_uses_explicit_opening_inventory_not_invented_shares():
    plan = {"date": DAY, "data_asof": "2026-09-18", "market_ok": False,
            "proposals": [sell_plan(direction="减持", qty=200)]}
    positions = {"600036": {"qty": 500, "cost": 10, "buy_day": "2026-09-18"}}
    original = deepcopy(positions)
    report = build_counterfactual(plan, {"600036": bar()}, initial_cash=95000, initial_positions=positions)
    assert positions == original
    for name in ("gate", "no_gate"):
        result = report["portfolios"][name]
        assert result["orders"][0]["status"] == "filled"
        assert result["orders"][0]["qty"] == 200
        assert result["initial_equity"] == 100000
        assert result["positions"]["600036"]["qty"] == 300
    cash_only = build_counterfactual(plan, {"600036": bar()})
    assert cash_only["portfolios"]["no_gate"]["orders"][0]["status"] == "rejected"


def test_intraday_exit_proceeds_do_not_finance_same_day_buy(tmp_path):
    path = tmp_path / "paper.db"
    seed(path, cash=0, qty=10000)
    buy = {"code": "601318", "direction": "买入", "position_pct": .1, "entry": [10],
           "entry_type": "limit", "executable": True}
    plans = [sell_plan(kind="limit", price=10.5), buy]
    _execute_portfolio(decision(plans), "no_gate", path, {"600036": bar(), "601318": bar()}, True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT side FROM trades WHERE day=?", (DAY,)).fetchall() == [("sell",)]
        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone() == (0,)


@pytest.mark.parametrize("field", ["quantity", "approval", "executable", "market", "date", "duplicate"])
def test_public_paper_rejects_decision_tampering_before_account_creation(tmp_path, monkeypatch, field):
    from trading_team.paper_pipeline import run_paper_day, paper_db_paths
    team = tmp_path / "trading_team"
    out = team / "outputs" / DAY
    out.mkdir(parents=True)
    plan = sell_plan(direction="减持", qty=100)
    for key in ("approval", "approved_plan_hash", "executable"):
        plan.pop(key)
    (out / "plan.json").write_text(json.dumps(build_reviewed_plan(DAY, "2026-09-18", False, [plan],
        [{"code": "600036", "decision": "通过", "reason_codes": []}])))
    document = materialize_paper_decision(DAY, team)
    row = document["portfolios"]["no_gate"]["plans"][0]
    if field == "quantity":
        row["exit"]["qty"] = 400
    elif field == "approval":
        row["approved_plan_hash"] = "forged"
    elif field == "executable":
        row["executable"] = False
    elif field == "market":
        document["portfolios"]["gate"]["market_ok"] = True
    elif field == "date":
        document["eligible_execution_day"] = "2026-09-22"
    else:
        document["portfolios"]["no_gate"]["plans"].append(deepcopy(row))
    (team / "decisions" / DAY / "paper_decision.json").write_text(json.dumps(document))
    monkeypatch.setattr("trading_team.paper_pipeline.load_execution_bars", lambda *_: {"600036": bar()})
    with pytest.raises(ValueError, match="Paper decision"):
        run_paper_day(DAY, team=team)
    assert all(not path.exists() for path in paper_db_paths(team).values())


def test_risk_rejection_removes_exit_intent_not_liquidates():
    from trading_team.kimi_daily import _apply_risk_reviews
    raw = sell_plan()
    for key in ("approval", "approved_plan_hash", "executable"):
        raw.pop(key)
    rejected = _apply_risk_reviews([raw], [{"code": "600036", "decision": "否决",
         "reason_codes": ["other"], "reason": "test rejection"}])[0]
    assert rejected["direction"] == "回避"
    assert rejected.get("exit") is None
    assert not is_sell(rejected["direction"])


@pytest.mark.parametrize("review", [{"clause": "条款4自动冻结"}, {"reason": "market_ok=false"}])
def test_no_gate_never_restores_a_risk_rejected_exit(review):
    from trading_team.kimi_daily import _apply_risk_reviews
    from trading_team.paper_plans import derive_paper_proposals

    raw = sell_plan()
    for key in ("approval", "approved_plan_hash", "executable"):
        raw.pop(key)
    rejected = _apply_risk_reviews([raw], [{"code": "600036", "decision": "否决",
         "reason_codes": ["other"], "reason": "test rejection", **review}])
    derived = derive_paper_proposals([raw], rejected)[0]
    assert derived["direction"] == "回避"
    assert derived.get("exit") is None


def test_buy_style_reduced_approval_does_not_authorize_full_exit():
    executable, reason = compute_executable(sell_plan(approval="reduced"), True, "v1")
    assert not executable and "exit contract" in reason


def test_duplicate_buy_and_exit_intents_are_rejected():
    from trading_team.paper_plans import select_paper_plans
    source = [sell_plan(), {"code": "600036", "direction": "买入", "position_pct": .1,
                           "entry": [10], "entry_type": "limit"}]
    with pytest.raises(ValueError, match="duplicate"):
        select_paper_plans({"proposals": source})
