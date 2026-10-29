import hashlib
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trading_team.risk_contract import build_reviewed_plan

DAYS = ["2026-09-17", "2026-09-18", "2026-09-21"]
NOW = datetime(2026, 9, 22, 8, tzinfo=ZoneInfo("Asia/Shanghai"))


def buy(price=10):
    return {"code": "600036", "direction": "买入", "position_pct": .1,
            "entry": [price], "entry_type": "limit"}


def sell(price=None):
    order = {"type": "market" if price is None else "limit", "qty": "all", "time_in_force": "DAY"}
    if price is not None:
        order["price"] = price
    return {"code": "600036", "direction": "卖出", "position_pct": 0, "exit": order}


def plan(team, day, previous, proposals, gate=True, reject=False):
    reviews = [{"code": p["code"], "decision": "否决" if reject or not gate and p["direction"] == "买入" else "通过",
                "reason_codes": ["thesis_invalidated"] if reject else ["market_gate"] if not gate and p["direction"] == "买入" else [],
                "reason": "test review"} for p in proposals]
    document = build_reviewed_plan(day, previous, gate, proposals, reviews)
    path = team / "outputs" / day / "plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return document


def prices(price, previous):
    return {"open": price, "high": price + .1, "low": price - .1, "close": price,
            "prev_close": previous}


@pytest.fixture
def series(tmp_path, execution_cache, finalize_calendar):
    import numpy as np
    calendar = tmp_path / "series-calendar.npy"
    np.save(calendar, np.array([20260902,20260903,20260916,20260917,20260918,20260921,20260922]))
    team = tmp_path / "trading_team"
    plan(team, DAYS[0], "2026-09-16", [buy()], gate=False)
    plan(team, DAYS[1], DAYS[0], [])
    plan(team, DAYS[2], DAYS[1], [sell()])
    for day, price, previous, index, index_previous in zip(DAYS, [10, 10.5, 10.8], [10, 10, 10.5],
                                                         [110, 99, 100], [100, 110, 99]):
        execution_cache(team, day, {"600036": prices(price, previous), "000300": prices(index, index_previous)})
    return team, calendar


def build(series, **kwargs):
    from trading_team.continuous_counterfactual import build_series
    team, calendar = series
    return build_series(DAYS[0], DAYS[-1], team=team, calendar_path=calendar, now=NOW, **kwargs)


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def test_cash_inventory_and_benchmark_are_continuous_and_readonly(series):
    before = hashes(series[0])
    report = build(series)
    assert report["status"] == "complete" and report["covered_sessions"] == 3
    first, hold, exit_day = report["rows"]
    assert first["portfolios"]["gate"]["orders"][0]["status"] == "blocked"
    acquired = first["portfolios"]["no_gate"]
    assert acquired["positions"]["600036"]["qty"] == 1000
    carried = hold["portfolios"]["no_gate"]
    assert carried["cash"] == acquired["cash"]
    assert carried["equity"] - acquired["equity"] == 500
    assert carried["orders"] == []
    assert exit_day["portfolios"]["no_gate"]["positions"] == {}
    assert exit_day["portfolios"]["no_gate"]["orders"][0]["side"] == "sell"
    assert report["summary"]["csi300"]["return"] == 0
    assert hold["portfolios"]["csi300"]["cumulative_return"] == -.01
    assert report["evidence_kind"] == "CONTINUOUS_DAILY_BAR_COUNTERFACTUAL"
    assert report["forward_performance"] == "unverified"
    assert report["production_account_used"] is False
    assert hashes(series[0]) == before
    assert not (series[0].parent / "paper").exists()
    assert build(series)["report_hash"] == report["report_hash"]


def test_matches_independent_existing_paper_engine_day_by_day(series, tmp_path):
    from trading_team.paper_pipeline import _execute_portfolio, materialize_paper_decision
    from trading_team.execution_bars import _cached_bars
    report = build(series)
    team, _ = series
    for row in report["rows"]:
        day = row["date"]
        decision = materialize_paper_decision(day, team)
        for name in ("gate", "no_gate"):
            path = tmp_path / "independent" / f"{name}.db"
            _execute_portfolio(decision, name, path, _cached_bars(day, team), True)
            with sqlite3.connect(path) as conn:
                assert conn.execute("SELECT cash,equity FROM account WHERE day=?", (day,)).fetchone() == (
                    row["portfolios"][name]["cash"], row["portfolios"][name]["equity"])


def test_addition_preserves_lots_in_continuous_experiment(series):
    team, _ = series
    plan(team, DAYS[0], "2026-09-16", [{**buy(), "position_pct": .05}], gate=True)
    plan(team, DAYS[1], DAYS[0], [buy(10.5)], gate=True)
    result = build(series)
    assert result["status"] == "complete"
    for name in ("gate", "no_gate"):
        second = result["rows"][1]["portfolios"][name]["positions"]["600036"]
        assert (second["qty"], second["available_qty"], second["frozen_qty"]) == (900, 500, 400)
        assert [lot["qty"] for lot in second["lots"]] == [500, 400]


@pytest.mark.parametrize("change", ["missing_plan", "missing_market", "tampered_market", "missing_holding",
                                   "same_day", "legacy_risk", "price_gap", "benchmark_gap"])
def test_mid_series_gap_never_becomes_zero_return_or_restart(series, execution_cache, change):
    team, _ = series
    day = DAYS[1]
    if change == "missing_plan":
        (team / "outputs" / day / "plan.json").unlink()
    elif change in ("missing_market", "tampered_market"):
        path = team / "replay_data" / f"{day}.json"
        if change == "missing_market":
            path.unlink()
        else:
            path.write_text("corrupt")
    elif change in ("same_day", "legacy_risk"):
        path = team / "outputs" / day / "plan.json"
        document = json.loads(path.read_text())
        if change == "same_day":
            document["data_asof"] = day
        else:
            document = {k: document[k] for k in ("date", "data_asof", "market_ok", "proposals")}
        path.write_text(json.dumps(document))
    else:
        stocks = {} if change == "missing_holding" else {"600036": prices(10.5, 10.1 if change == "price_gap" else 10)}
        execution_cache(team, day, {**stocks, "000300": prices(99, 111 if change == "benchmark_gap" else 110)})
    report = build(series)
    assert report["status"] == "incomplete" and report["covered_sessions"] == 1
    assert report["summary"] is None
    assert report["rows"][1]["status"] == "gap" and report["rows"][1]["reason"]
    assert report["rows"][1]["portfolios"] is None
    assert report["rows"][2]["status"] == "not_evaluated"


def test_no_gate_does_not_restore_non_gate_risk_rejection(series):
    plan(series[0], DAYS[0], "2026-09-16", [buy()], gate=False, reject=True)
    report = build(series)
    assert all(row["portfolios"][name]["positions"] == {} for row in report["rows"] for name in ("gate","no_gate"))


def test_unfilled_exit_preserves_inventory_without_opportunity_cost(series):
    plan(series[0], DAYS[-1], DAYS[-2], [sell(11)])
    report = build(series)
    last = report["rows"][-1]["portfolios"]["no_gate"]
    assert last["orders"][0]["status"] == "unfilled"
    assert last["positions"]["600036"]["qty"] == 1000
    assert "opportunity_cost" not in json.dumps(report)


def test_future_session_not_evaluated_even_with_complete_files(series):
    from trading_team.continuous_counterfactual import build_series
    team, calendar = series
    report = build_series(DAYS[0], DAYS[-1], team=team, calendar_path=calendar,
                          now=datetime(2026,9,21,10,tzinfo=ZoneInfo("Asia/Shanghai")))
    assert report["covered_sessions"] == 2 and report["summary"] is None
    assert "completed" in report["rows"][-1]["reason"]


def test_plan_version_change_changes_input_and_result_hashes(series):
    first = build(series)
    plan(series[0], DAYS[0], "2026-09-16", [buy(9.5)], gate=False)
    second = build(series)
    assert first["input_hash"] != second["input_hash"]
    assert first["report_hash"] != second["report_hash"]
    assert second["rows"][0]["portfolios"]["no_gate"]["orders"][0]["status"] == "unfilled"


def test_invalid_range_or_missing_calendar_is_not_empty_success(series, tmp_path):
    from trading_team.continuous_counterfactual import build_series
    with pytest.raises(ValueError):
        build_series(DAYS[-1], DAYS[0], team=series[0], calendar_path=series[1], now=NOW)
    with pytest.raises((ValueError, OSError)):
        build_series(DAYS[0], DAYS[-1], team=series[0], calendar_path=tmp_path / "missing", now=NOW)


def test_writer_and_daily_replay_publish_separate_report_with_prior_inputs(series, monkeypatch):
    from trading_team import counterfactual, execution_bars
    from trading_team.daily_finalize import _step_evidence
    monkeypatch.setattr(execution_bars, "DEFAULT_TRADING_CALENDAR", series[1])
    team, _ = series
    report = counterfactual.write_counterfactual(DAYS[-1], team)
    path = team / "outputs" / DAYS[-1] / "continuous_counterfactual.json"
    assert path.exists()
    continuous = json.loads(path.read_text())
    assert continuous["report_hash"] == report["continuous_report_hash"]
    before = _step_evidence("counterfactual", report, DAYS[-1], report["plan_hash"], team)
    prior = team / "outputs" / DAYS[0] / "plan.json"
    prior.write_text(prior.read_text() + " ")
    after = _step_evidence("counterfactual", report, DAYS[-1], report["plan_hash"], team)
    assert before != after


def test_mutation_during_evaluation_is_rejected_not_published(series, monkeypatch):
    from trading_team import continuous_counterfactual as module
    real = module._execute_portfolio_transaction
    path = series[0] / "outputs" / DAYS[0] / "plan.json"
    def mutate(*args, **kwargs):
        result = real(*args, **kwargs)
        path.write_text(path.read_text() + " ")
        return result
    monkeypatch.setattr(module, "_execute_portfolio_transaction", mutate)
    with pytest.raises(ValueError, match="changed"):
        build(series)


def test_default_view_ends_at_completed_session(series):
    from trading_team.continuous_counterfactual import current_series
    result = current_series(team=series[0], start=DAYS[0], calendar_path=series[1], now=NOW)
    assert result["end"] == DAYS[-1] and result["status"] == "complete"


def test_api_is_readonly_and_keeps_counterfactual_distinct(series, monkeypatch):
    import importlib.util
    from pathlib import Path
    from trading_team import continuous_counterfactual
    spec = importlib.util.spec_from_file_location("continuous_console", Path(__file__).resolve().parents[1] / "console/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", series[0].parent)
    observed = []
    def current_series(**kwargs):
        observed.append(kwargs)
        return {"status":"incomplete", "summary":None}
    monkeypatch.setattr(continuous_counterfactual, "current_series", current_series)
    handler = object.__new__(module.H)
    handler.path = f"/api/continuous-counterfactual?start={DAYS[0]}&end={DAYS[-1]}"
    sent = []
    handler._send_json = lambda status, data: sent.append((status,data))
    before = hashes(series[0])
    handler.do_GET()
    assert sent == [(200,{"ok":True,"status":"incomplete","summary":None})]
    assert observed == [{"team":series[0],"start":DAYS[0],"end":DAYS[-1]}]
    assert hashes(series[0]) == before


def test_source_directory_symlink_is_not_followed(series, tmp_path):
    team, _ = series
    original = team / "outputs" / DAYS[1]
    moved = tmp_path / "outside"
    original.rename(moved)
    original.symlink_to(moved)
    with pytest.raises(ValueError, match="symlink"):
        build(series)


def test_runtime_and_completed_session_boundary_are_input_versions(series, monkeypatch):
    from trading_team import continuous_counterfactual as module
    original = build(series)
    fingerprints = module.runtime_fingerprints()
    assert fingerprints
    monkeypatch.setattr(module, "runtime_fingerprints", lambda: {**fingerprints,"paper/execution_model.py":"0"*64})
    changed = build(series)
    assert original["input_hash"] != changed["input_hash"]
    earlier = module.build_series(DAYS[0], DAYS[-1], team=series[0], calendar_path=series[1],
                                  now=datetime(2026,9,21,10,tzinfo=ZoneInfo("Asia/Shanghai")))
    assert earlier["input_hash"] != changed["input_hash"]


@pytest.mark.parametrize("state", ["suspended", "limit_up", "untouched"])
def test_no_gate_retains_execution_constraints(series, execution_cache, state):
    values = prices(10,10)
    if state == "suspended":
        values.update(suspended=True,volume=0)
    elif state == "limit_up":
        values = {**prices(11,10),"high":11,"low":11}
    else:
        plan(series[0],DAYS[0],"2026-09-16",[buy(9.5)],gate=False)
    execution_cache(series[0],DAYS[0],{"600036":values,"000300":prices(110,100)})
    report = build(series)
    order = report["rows"][0]["portfolios"]["no_gate"]["orders"][0]
    assert order["status"] != "filled" and order["fee"] == 0
    assert report["rows"][0]["portfolios"]["no_gate"]["positions"] == {}
