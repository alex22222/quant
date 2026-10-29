"""Standing Paper instructions must retain explicit authority across sessions."""
import json
import sqlite3

import pytest

from trading_team.paper_pipeline import _db, _execute_portfolio, build_paper_decision
from trading_team.risk_contract import build_reviewed_plan
from trading_team.schemas import compute_executable, validate_plan
from tests.test_exit_contract import bar, seed, sell_plan


def proposal(action="create", expected=None, **changes):
    command = {"action": action, "expected_order_hash": expected}
    if action != "cancel":
        command.update(type="stop", price=9.3, qty="all_existing", valid_until="2026-09-30")
    command.update(changes)
    return {"code": "600036", "direction": "持有", "position_pct": 0,
            "entry": None, "protection": command}


def decision(day, proposals):
    return build_paper_decision(build_reviewed_plan(day, "2026-09-18", True, proposals,
        [{"code": p["code"], "decision": "通过", "reason_codes": []} for p in proposals]))


def run(path, day, plans, **market):
    doc = decision(day, plans)
    return _execute_portfolio(doc, "no_gate", path, {"600036": bar(**{"close": 10, **market})}, True)


def receipt(path, day):
    with sqlite3.connect(path) as conn:
        return json.loads(conn.execute("SELECT result_json FROM execution_receipts WHERE execution_day=?", (day,)).fetchone()[0])


def active(path, day):
    return receipt(path, day)["protection"]["closing"]


@pytest.mark.parametrize("changes", [
    {"type": "market"}, {"qty": "all"}, {"price": True}, {"price": 9.333},
    {"valid_until": "tomorrow"}, {"time_in_force": "GTC"}, {"expected_order_hash": "stale"},
])
def test_malformed_protection_is_not_silently_ignored(changes):
    plan = proposal(**changes)
    assert validate_plan(plan, strict=True)
    plan.update(approval="approved", approval_source="paper_auto", approved_plan_hash="v1")
    assert not compute_executable(plan, True, "v1")[0]


def test_new_protection_survives_omitted_plan_and_fills_only_when_triggered(tmp_path):
    path = tmp_path / "paper.db"
    seed(path)
    first = run(path, "2026-09-21", [proposal()])
    assert first["orders"] == 1 and first["fills"] == 0
    order = active(path, "2026-09-21")["600036"]
    assert order["qty"] == 500 and order["plan_hash"] == decision("2026-09-21", [proposal()])["plan_hash"]
    run(path, "2026-09-22", [])
    assert active(path, "2026-09-22") == {"600036": order}
    result = run(path, "2026-09-23", [], open=9.2, low=9.1, close=9.4)
    assert result["fills"] == 1 and active(path, "2026-09-23") == {}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM pending_orders ORDER BY id").fetchall() == [("unfilled",), ("unfilled",), ("filled",)]
        assert conn.execute("SELECT price FROM trades WHERE side='sell'").fetchone() == (9.18,)
    before = path.read_bytes()
    assert run(path, "2026-09-23", [], open=9.2, low=9.1, close=9.4)["status"] == "skipped"
    assert path.read_bytes() == before


def test_replace_cancel_compare_exact_version_and_stale_action_does_not_disable_guard(tmp_path):
    path = tmp_path / "paper.db"
    seed(path)
    run(path, "2026-09-21", [proposal()])
    old = active(path, "2026-09-21")["600036"]["order_hash"]
    run(path, "2026-09-22", [proposal("replace", old, price=9.2)])
    new = active(path, "2026-09-22")["600036"]["order_hash"]
    assert new != old
    run(path, "2026-09-23", [proposal("cancel", old)])
    assert active(path, "2026-09-23")["600036"]["order_hash"] == new
    assert receipt(path, "2026-09-23")["protection"]["events"][0]["status"] == "stale_version"
    run(path, "2026-09-24", [proposal("cancel", new)], low=9.1)
    assert active(path, "2026-09-24") == {}
    assert receipt(path, "2026-09-24")["fills"] == 0


def test_expiry_is_inclusive_and_suspension_keeps_authority(tmp_path):
    path = tmp_path / "paper.db"
    seed(path)
    run(path, "2026-09-21", [proposal(valid_until="2026-09-22")])
    run(path, "2026-09-22", [], suspended=True, low=9.1)
    assert active(path, "2026-09-22")
    run(path, "2026-09-23", [], low=9.1)
    assert not active(path, "2026-09-23")
    assert receipt(path, "2026-09-23")["protection"]["events"][0]["status"] == "expired"


def test_missing_holding_does_not_create_future_buy_protection(tmp_path):
    path = tmp_path / "paper.db"
    _db(path).close()
    result = run(path, "2026-09-21", [proposal()])
    assert result["fills"] == result["orders"] == 0
    assert active(path, "2026-09-21") == {}
    assert receipt(path, "2026-09-21")["protection"]["events"][0]["status"] == "no_position"


def test_reference_stop_and_rejected_protection_cannot_authorize_an_exit(tmp_path):
    raw = proposal()
    denied = build_reviewed_plan("2026-09-21", "2026-09-18", True, [raw],
        [{"code": "600036", "decision": "否决", "reason_codes": ["other"], "reason": "not authorized"}])
    assert "protection" not in denied["proposals"][0]
    path = tmp_path / "paper.db"
    seed(path)
    result = run(path, "2026-09-21", [{"code": "600036", "direction": "持有", "position_pct": 0, "stop": 10}])
    assert result["orders"] == 0 and active(path, "2026-09-21") == {}


@pytest.mark.parametrize("field", ["closing", "plans", "events", "drop"])
def test_changed_protection_receipt_blocks_retry_without_writes(tmp_path, field):
    path = tmp_path / "paper.db"
    seed(path)
    run(path, "2026-09-21", [proposal()])
    data = receipt(path, "2026-09-21")
    if field == "closing":
        data["protection"]["closing"]["600036"]["price"] = 9.2
    elif field == "plans":
        data["protection"]["plans"][0]["approved_plan_hash"] = "forged"
    elif field == "events":
        data["protection"]["events"] = []
    else:
        data.pop("protection")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE execution_receipts SET result_json=?", (json.dumps(data),))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="protection"):
        run(path, "2026-09-21", [proposal()])
    assert path.read_bytes() == before


def test_cancellation_can_authorize_an_explicit_day_exit(tmp_path):
    path = tmp_path / "paper.db"
    seed(path)
    run(path, "2026-09-21", [proposal()])
    guard = active(path, "2026-09-21")["600036"]["order_hash"]
    plan = {k: v for k, v in sell_plan().items() if k not in ("approval", "approved_plan_hash", "executable")}
    plan["protection"] = proposal("cancel", guard)["protection"]
    result = run(path, "2026-09-22", [plan])
    assert result["orders"] == result["fills"] == 1
    assert active(path, "2026-09-22") == {}


def test_same_symbol_trade_requires_explicit_guard_cancellation(tmp_path):
    path = tmp_path / "paper.db"
    seed(path)
    run(path, "2026-09-21", [proposal()])
    plan = {k: v for k, v in sell_plan().items() if k not in ("approval", "approved_plan_hash", "executable")}
    before = path.read_bytes()
    with pytest.raises(ValueError, match="active protection"):
        run(path, "2026-09-22", [plan])
    assert path.read_bytes() == before


def test_non_paper_approval_cannot_enable_standing_protection():
    plan = proposal()
    plan.update(approval="approved", approved_plan_hash="v1")
    assert not compute_executable(plan, True, "v1")[0]


@pytest.fixture
def protected_team(tmp_path, execution_cache, monkeypatch, stub_research_reviews):
    import numpy as np
    from trading_team import execution_bars
    from trading_team.paper_pipeline import materialize_paper_decision, paper_db_paths
    from tests.test_continuous_counterfactual import plan, buy
    team = tmp_path / "trading_team"
    calendar = tmp_path / "calendar.npy"
    np.save(calendar, np.array([20260916,20260917,20260918,20260921,20260922]))
    monkeypatch.setattr(execution_bars, "DEFAULT_TRADING_CALENDAR", calendar)
    for day, previous, candidates in [("2026-09-17", "2026-09-16", [buy()]),
                                      ("2026-09-18", "2026-09-17", [proposal()]),
                                      ("2026-09-21", "2026-09-18", [])]:
        plan(team, day, previous, candidates)
        execution_cache(team, day, {code: as_bar for code, as_bar in (
            ("600036", {"open": 10, "high": 10.2, "low": 9.1 if day == "2026-09-21" else 9.4, "close": 10, "prev_close": 10}),
            ("000300", {"open": 100, "high": 101, "low": 99, "close": 100, "prev_close": 100}))})
        doc = materialize_paper_decision(day, team)
        if day != "2026-09-21":
            for name, path in paper_db_paths(team).items():
                _execute_portfolio(doc, name, path, execution_bars._cached_bars(day, team), True)
    return team, calendar


def test_public_entry_checks_original_authority_before_either_account_changes(protected_team):
    from trading_team.paper_pipeline import run_paper_day, paper_db_paths
    team, _ = protected_team
    source = team / "outputs/2026-09-18/plan.json"
    data = json.loads(source.read_text())
    data["market_ok"] = False
    source.write_text(json.dumps(data))
    before = {k: p.read_bytes() for k, p in paper_db_paths(team).items()}
    with pytest.raises(ValueError, match="protection source"):
        run_paper_day("2026-09-21", team=team)
    assert before == {k: p.read_bytes() for k, p in paper_db_paths(team).items()}


def test_receipt_cache_rechecks_original_authority(protected_team):
    from trading_team.daily_finalize import _paper_evidence
    team, _ = protected_team
    doc = json.loads((team / "decisions/2026-09-18/paper_decision.json").read_text())
    source = team / "outputs/2026-09-18/plan.json"
    data = json.loads(source.read_text())
    data["market_ok"] = False
    source.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="protection source"):
        _paper_evidence("2026-09-18", doc["plan_hash"], team)


def test_public_entry_refuses_to_skip_an_active_guard_session(protected_team):
    from trading_team.paper_pipeline import run_paper_day, materialize_paper_decision, paper_db_paths
    from tests.test_continuous_counterfactual import plan
    team, _ = protected_team
    plan(team, "2026-09-22", "2026-09-21", [])
    materialize_paper_decision("2026-09-22", team)
    before = {k: p.read_bytes() for k, p in paper_db_paths(team).items()}
    with pytest.raises(ValueError, match="protection.*session"):
        run_paper_day("2026-09-22", team=team)
    assert before == {k: p.read_bytes() for k, p in paper_db_paths(team).items()}


def test_continuous_replay_and_auditor_recognize_standing_exit_authority(protected_team):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from trading_team.paper_pipeline import run_paper_day, paper_db_paths
    from trading_team.paper_reconcile import reconcile_accounts
    from trading_team.continuous_counterfactual import build_series
    team, calendar = protected_team
    now = datetime(2026, 9, 22, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
    run_paper_day("2026-09-21", team=team)
    series = build_series("2026-09-17", "2026-09-21", team=team, calendar_path=calendar, now=now)
    assert series["status"] == "complete"
    report = reconcile_accounts(team=team, calendar_path=calendar, now=now)
    for name, path in paper_db_paths(team).items():
        item = report["portfolios"][name]
        assert item["conflict_count"] == 0
        assert item["days"]["2026-09-21"]["price_replay"] == "matched_recorded_quantity"
        assert item["checks"]["protection"] == "matched_sources"
        last = series["rows"][-1]["portfolios"][name]
        assert last["protection"]["active"] == {}
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT cash,equity FROM account WHERE day='2026-09-21'").fetchone() == (last["cash"], last["equity"])


def test_snapshot_distinguishes_active_from_unknown_protection(protected_team):
    from trading_team.paper_pipeline import paper_db_paths, account_snapshot
    team, _ = protected_team
    path = paper_db_paths(team)["no_gate"]
    before = path.read_bytes()
    snapshot = account_snapshot(path, as_of_day="2026-09-21")["protection"]
    assert snapshot["status"] == "matched_receipts" and snapshot["active"]["600036"]["qty"] == 1000
    assert snapshot["execution_certified"] is False
    assert path.read_bytes() == before
    data = receipt(path, "2026-09-18")
    data["protection"]["closing"] = {}
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE execution_receipts SET result_json=? WHERE execution_day='2026-09-18'", (json.dumps(data),))
    snapshot = account_snapshot(path, as_of_day="2026-09-21")["protection"]
    assert snapshot["status"] == "unverified" and snapshot["active"] is None


def test_single_day_diagnostic_does_not_invent_standing_order_state():
    from trading_team.counterfactual import build_counterfactual, render_markdown
    plan = build_reviewed_plan("2026-09-21", "2026-09-18", True, [proposal()],
                              [{"code": "600036", "decision": "通过", "reason_codes": []}])
    report = build_counterfactual(plan, {"600036": bar()})
    for name in ("gate", "no_gate"):
        assert report["portfolios"][name]["return"] is None
        assert report["portfolios"][name]["equity"] is None
        assert report["portfolios"][name]["status"] == "requires_continuous_state"
    assert "连续" in render_markdown(report)


def test_deleted_guard_receipts_cannot_silently_remove_protection(protected_team):
    from trading_team.paper_pipeline import run_paper_day, paper_db_paths
    team, _ = protected_team
    path = paper_db_paths(team)["gate"]
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM execution_receipts WHERE execution_day='2026-09-18'")
    before = {k: p.read_bytes() for k, p in paper_db_paths(team).items()}
    with pytest.raises(ValueError, match="protection"):
        run_paper_day("2026-09-21", team=team)
    assert before == {k: p.read_bytes() for k, p in paper_db_paths(team).items()}


def test_frozen_existing_lot_is_not_sold_or_resized_by_guard():
    from trading_team.paper_protection import prepare_protection
    plan = decision("2026-09-21", [proposal()])["portfolios"]["no_gate"]["plans"][0]
    position = {"qty": 500, "buy_day": "2026-09-21",
                "lots": [{"trade_id": 1, "buy_day": "2026-09-18", "qty": 300},
                         {"trade_id": 2, "buy_day": "2026-09-21", "qty": 200}]}
    first, _ = prepare_protection([plan], {"600036": position}, {}, "2026-09-21",
                                  plan["approved_plan_hash"], {"600036": bar(low=9.1)}, slippage_rate=.002)
    assert first["events"][-1]["fill"]["status"] == "rejected"
    assert first["closing"]["600036"]["qty"] == 500
    second, _ = prepare_protection([], {"600036": position}, first["closing"], "2026-09-22",
                                   "next-plan", {"600036": bar(low=9.1)}, slippage_rate=.002)
    assert second["events"][-1]["fill"]["qty"] == 500 and second["closing"] == {}


@pytest.mark.parametrize("damage", ["input_hash", "input_type", "current_position"])
def test_guard_history_and_live_projection_are_checked_before_new_session(protected_team, damage):
    from trading_team.paper_pipeline import paper_db_paths, run_paper_day
    team, _ = protected_team
    path = paper_db_paths(team)["no_gate"]
    data = receipt(path, "2026-09-18")
    with sqlite3.connect(path) as conn:
        if damage == "current_position":
            conn.execute("UPDATE positions SET qty=1100")
        else:
            if damage == "input_hash":
                data["execution_inputs_hash"] = "bad"
            else:
                data["execution_inputs"] = []
            conn.execute("UPDATE execution_receipts SET result_json=? WHERE execution_day='2026-09-18'", (json.dumps(data),))
    before = {k: p.read_bytes() for k, p in paper_db_paths(team).items()}
    with pytest.raises(ValueError, match="protection|inventory"):
        run_paper_day("2026-09-21", team=team)
    assert before == {k: p.read_bytes() for k, p in paper_db_paths(team).items()}


def test_guard_receipt_failure_rolls_back_all_account_changes(protected_team, monkeypatch):
    import trading_team.paper_pipeline as pipeline
    team, _ = protected_team
    path = pipeline.paper_db_paths(team)["no_gate"]
    doc = json.loads((team / "decisions/2026-09-21/paper_decision.json").read_text())
    original = pipeline._execution_inputs
    def fail_after_fills(bars, **kwargs):
        raise RuntimeError("receipt construction failure")
    before = path.read_bytes()
    monkeypatch.setattr(pipeline, "_execution_inputs", fail_after_fills)
    with pytest.raises(RuntimeError, match="receipt construction"):
        _execute_portfolio(doc, "no_gate", path, {"600036": bar(low=9.1)}, True)
    monkeypatch.setattr(pipeline, "_execution_inputs", original)
    assert path.read_bytes() == before


def test_reference_price_discontinuity_blocks_protection_before_any_fill(tmp_path):
    path = tmp_path / "paper.db"
    seed(path)
    run(path, "2026-09-21", [proposal()])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="protection.*reference"):
        run(path, "2026-09-22", [], prev_close=9.5, open=9.3, low=9.1)
    assert path.read_bytes() == before


def test_workflow_restore_retains_authority_and_no_duplicate_guard_fill(protected_team, tmp_path_factory):
    from trading_team.workflow_recovery import backup_workflow, restore_workflow
    from trading_team.run_ledger import RunLedger
    from trading_team.paper_pipeline import paper_db_paths, run_paper_day
    team, _ = protected_team
    destination = tmp_path_factory.mktemp("guard-recovery")
    RunLedger(team / ".runs/ledger.db")
    saved = backup_workflow(team=team, target=destination / "backup")
    restore_workflow(destination / "backup", saved["manifest_hash"], destination / "restored")
    recovered = destination / "restored/trading_team"
    before = {name: p.read_bytes() for name, p in paper_db_paths(recovered).items()}
    retried = run_paper_day("2026-09-18", team=recovered)
    assert all(row["status"] == "skipped" for row in retried.values())
    assert before == {name: p.read_bytes() for name, p in paper_db_paths(recovered).items()}
    for target in (team, recovered):
        run_paper_day("2026-09-21", team=target)
        for path in paper_db_paths(target).values():
            with sqlite3.connect(path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM trades WHERE side='sell'").fetchone() == (1,)
            assert active(path, "2026-09-21") == {}


@pytest.mark.parametrize("bad_plans", [None, {}, ["not-a-plan"]])
def test_malformed_receipt_plans_return_unknown_instead_of_crashing_snapshot(protected_team, bad_plans):
    from trading_team.paper_pipeline import paper_db_paths, account_snapshot
    team, _ = protected_team
    path = paper_db_paths(team)["gate"]
    data = receipt(path, "2026-09-18")
    data["protection"]["plans"] = bad_plans
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE execution_receipts SET result_json=? WHERE execution_day='2026-09-18'", (json.dumps(data),))
    assert account_snapshot(path, as_of_day="2026-09-21")["protection"]["status"] == "unverified"
