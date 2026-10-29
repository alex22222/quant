import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pytest

from trading_team import daily_finalize
from trading_team.paper_pipeline import _db, paper_db_paths
from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.run_ledger import RunLedger

pytestmark = pytest.mark.usefixtures("finalize_calendar")


DAY = "2026-09-21"


@pytest.fixture
def workspace(tmp_path, execution_cache, stub_research_reviews):
    team = tmp_path / "trading_team"
    output = team / "outputs" / DAY
    output.mkdir(parents=True)
    (output / "plan.json").write_text(json.dumps({
        "date": DAY, "data_asof": "2026-09-18", "market_ok": False,
        "proposals": [], "paper_proposals": [],
    }))
    execution_cache(team, DAY, {"000300": {
            "open": 100, "high": 103, "low": 99, "close": 102, "prev_close": 100,
    }})
    return team, RunLedger(team / ".runs/ledger.db")


def test_completed_zero_order_run_reuses_without_rewriting_account(workspace):
    team, ledger = workspace
    first = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    second = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    assert first["status"] == "succeeded"
    assert second["status"] == "reused"
    for path in paper_db_paths(team).values():
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 1


def test_daily_scope_does_not_recertify_unrelated_legacy_history(workspace):
    team, ledger = workspace
    legacy = team / "outputs/2026-08-24/plan.json"
    legacy.parent.mkdir()
    legacy.write_text("legacy damaged plan")
    results = daily_finalize.finalize_available_tracked(team=team, ledger=ledger, focus_day=DAY)
    by_day = {result["date"]: result for result in results}
    assert set(by_day) == {"2026-09-18", DAY}
    assert by_day["2026-09-18"]["status"] == "skipped"
    assert by_day[DAY]["status"] == "succeeded"
    assert all(run["business_date"] == DAY for run in ledger.list_runs())
    assert legacy.read_text() == "legacy damaged plan"


@pytest.mark.parametrize("damage", ["missing", "metadata"])
def test_continuous_report_damage_is_rebuilt_without_account_changes(workspace, damage):
    team, ledger = workspace
    first = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    before = {name: path.read_bytes() for name,path in paper_db_paths(team).items()}
    path = team / "outputs" / DAY / "continuous_counterfactual.json"
    if damage == "missing":
        path.unlink()
    else:
        report = json.loads(path.read_text())
        report.pop("inputs")
        path.write_text(json.dumps(report))
    result = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    assert result["status"] == "succeeded" and result["run_id"] != first["run_id"]
    assert json.loads(path.read_text())["inputs"]["files"]
    assert {name: path.read_bytes() for name,path in paper_db_paths(team).items()} == before


def test_finalize_cache_does_not_hide_execution_policy_change(workspace, monkeypatch):
    from trading_team import paper_pipeline

    team, ledger = workspace
    daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    before = {name: path.read_bytes() for name, path in paper_db_paths(team).items()}
    monkeypatch.setattr(paper_pipeline, "EXECUTION_MODEL_VERSION", "changed")
    with pytest.raises(ValueError, match="execution evidence changed"):
        daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    assert {name: path.read_bytes() for name, path in paper_db_paths(team).items()} == before


@pytest.mark.parametrize("relative_path", [
    f"decisions/{DAY}/decision.json", f"decisions/{DAY}/paper_decision.json",
    f"outputs/{DAY}/counterfactual.json", f"outputs/{DAY}/counterfactual.md",
])
@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_missing_or_modified_derived_artifact_is_rebuilt(workspace, relative_path, damage):
    team, ledger = workspace
    first = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    target = team / relative_path
    if damage == "missing":
        target.unlink()
    elif target.suffix == ".md":
        target.write_text("corrupted replay")
    else:
        doc = json.loads(target.read_text())
        doc["plan_hash"] = "wrong-version"
        target.write_text(json.dumps(doc))
    second = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    assert second["status"] == "succeeded"
    assert first["run_id"] != second["run_id"]
    assert target.is_file()
    if target.suffix == ".json":
        assert json.loads(target.read_text())["plan_hash"] == first["plan_hash"]
    else:
        assert "corrupted" not in target.read_text()
    for path in paper_db_paths(team).values():
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 1


def test_changed_account_evidence_fails_without_replaying_trades(workspace):
    team, ledger = workspace
    daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    path = paper_db_paths(team)["no_gate"]
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE account SET cash=42 WHERE day=?", (DAY,))
    with pytest.raises(ValueError, match="Paper evidence"):
        daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    with pytest.raises(ValueError, match="Paper evidence"):
        daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    assert ledger.list_runs()[0]["status"] == "failed"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT cash FROM account WHERE day=?", (DAY,)).fetchone()[0] == 42


@pytest.mark.parametrize("damage", ["position", "prior_trade"])
def test_cached_finalize_rechecks_inventory_not_just_same_day_rows(workspace, damage):
    team, ledger = workspace
    daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    path = paper_db_paths(team)["no_gate"]
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO positions VALUES ('600036',100,10,'2026-09-18')")
        if damage == "prior_trade":
            conn.execute("INSERT INTO trades(day,code,side,qty) VALUES ('2026-09-18','600036','buy',100)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Paper evidence"):
        daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    assert path.read_bytes() == before


def test_failed_resume_rechecks_successful_materialization(workspace, monkeypatch):
    team, ledger = workspace
    real = daily_finalize.write_counterfactual
    def fail(*_args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(daily_finalize, "write_counterfactual", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger)
    (team / "decisions" / DAY / "paper_decision.json").unlink()
    monkeypatch.setattr(daily_finalize, "write_counterfactual", real)
    result = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger)
    assert result["status"] == "succeeded"
    assert (team / "decisions" / DAY / "paper_decision.json").is_file()


def test_legacy_plan_is_normalized_before_cache_versioning(workspace):
    team, ledger = workspace
    path = team / "outputs" / DAY / "plan.json"
    plan = json.loads(path.read_text())
    plan.pop("paper_proposals")
    path.write_text(json.dumps(plan))
    first = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, replay=False)
    second = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, replay=False)
    assert first["plan_hash"] == first["materialize"]["plan_hash"]
    assert second["status"] == "reused"


@pytest.mark.parametrize("hour,minute,should_execute", [(15, 9, False), (15, 10, True)])
def test_session_gate_observes_close_cutoff(workspace, tmp_path, hour, minute, should_execute):
    team, ledger = workspace
    calendar = tmp_path / "calendar.npy"
    np.save(calendar, np.array([20260918, 20260921, 20260922]))
    result = daily_finalize.run_finalize_workflow(
        DAY, team=team, ledger=ledger, paper=True,
        now=datetime(2026, 9, 21, hour, minute), calendar_path=calendar,
    )
    if should_execute:
        assert result["paper"]["gate"]["status"] == "succeeded"
    else:
        assert result["paper"]["status"] == "skipped"
        assert not paper_db_paths(team)["gate"].exists()


def test_weekday_holiday_never_enters_replay_or_paper(workspace, tmp_path):
    team, ledger = workspace
    day = "2026-10-01"
    output = team / "outputs" / day
    output.mkdir()
    (output / "plan.json").write_text(json.dumps({
        "date": day, "data_asof": "2026-09-30", "market_ok": True,
        "proposals": [], "paper_proposals": [],
    }))
    calendar = tmp_path / "calendar.npy"
    np.save(calendar, np.array([20260918, 20260921, 20260930, 20261009]))
    result = daily_finalize.run_finalize_workflow(
        day, team=team, ledger=ledger, paper=True,
        now=datetime(2026, 10, 2, 16), calendar_path=calendar,
    )
    assert result["counterfactual"]["status"] == "skipped"
    assert result["paper"]["status"] == "skipped"
    assert not paper_db_paths(team)["gate"].exists()


@pytest.mark.parametrize("kind", ["missing", "corrupt", "truncated", "expired", "empty"])
def test_unusable_calendar_fails_closed_and_is_recorded(workspace, tmp_path, kind):
    team, ledger = workspace
    calendar = tmp_path / "calendar.npy"
    if kind == "corrupt":
        calendar.write_bytes(b"not a calendar")
    elif kind == "truncated":
        calendar.write_bytes(b"")
    elif kind == "expired":
        np.save(calendar, np.array([20250102, 20251231]))
    elif kind == "empty":
        np.save(calendar, np.array([], dtype=int))
    with pytest.raises(ValueError, match="calendar"):
        daily_finalize.run_finalize_workflow(
            DAY, team=team, ledger=ledger, paper=True,
            now=datetime(2026, 9, 22), calendar_path=calendar,
        )
    assert ledger.list_runs()[0]["status"] == "failed"
    assert not paper_db_paths(team)["gate"].exists()


def test_batch_failure_produces_nonzero_exit(monkeypatch):
    monkeypatch.setattr("sys.argv", ["daily_finalize"])
    monkeypatch.setattr(daily_finalize, "finalize_available_tracked", lambda: [{"status": "failed"}])
    with pytest.raises(SystemExit) as exc:
        daily_finalize.main()
    assert exc.value.code == 1


def test_repair_does_not_repeat_filled_orders_or_fees(workspace, execution_cache):
    from trading_team.risk_contract import build_reviewed_plan
    team, ledger = workspace
    plan_path = team / "outputs" / DAY / "plan.json"
    plan = json.loads(plan_path.read_text())
    proposal = {
        "code": "600036", "name": "CMB", "direction": "条件买入",
        "entry": [40], "entry_type": "limit", "position_pct": 0.1,
        "stop": 37, "confidence": 4,
    }
    plan = build_reviewed_plan(DAY, plan["data_asof"], plan["market_ok"], [proposal],
                               [{"code": "600036", "decision": "通过", "reason_codes": []}])
    plan_path.write_text(json.dumps(plan))
    cache_path = team / "replay_data" / f"{DAY}.json"
    cache = json.loads(cache_path.read_text())
    cache["bars"]["600036"] = {"open": 39, "high": 41, "low": 38, "close": 40, "prev_close": 39}
    execution_cache(team, DAY, cache["bars"])
    first = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    assert first["paper"]["no_gate"]["fills"] == 1

    def account_facts():
        with sqlite3.connect(paper_db_paths(team)["no_gate"]) as conn:
            return (conn.execute("SELECT * FROM account ORDER BY rowid").fetchall(),
                    conn.execute("SELECT * FROM trades ORDER BY id").fetchall(),
                    conn.execute("SELECT * FROM positions").fetchall())

    before = account_facts()
    (team / "outputs" / DAY / "counterfactual.json").unlink()
    daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    assert account_facts() == before


def test_utc_clock_uses_shanghai_close_boundary(workspace, tmp_path):
    team, ledger = workspace
    calendar = tmp_path / "calendar.npy"
    np.save(calendar, np.array([20260918, 20260921, 20260922]))
    result = daily_finalize.run_finalize_workflow(
        DAY, team=team, ledger=ledger, paper=True, calendar_path=calendar,
        now=datetime(2026, 9, 21, 7, 9, tzinfo=timezone.utc),
    )
    assert result["paper"]["status"] == "skipped"
    assert not paper_db_paths(team)["gate"].exists()


def test_batch_routes_holidays_through_calendar(workspace, tmp_path):
    team, ledger = workspace
    output = team / "outputs" / DAY
    holiday = "2026-10-01"
    output.rename(output.parent / holiday)
    plan_path = output.parent / holiday / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan["date"] = holiday
    plan_path.write_text(json.dumps(plan))
    calendar = tmp_path / "calendar.npy"
    np.save(calendar, np.array([20260930, 20261009]))
    results = daily_finalize.finalize_available_tracked(
        team=team, ledger=ledger, now=datetime(2026, 10, 2, 16), calendar_path=calendar,
    )
    assert results[0]["status"] == "succeeded"
    assert results[0]["paper"]["reason"] == "exchange closed"
    assert not paper_db_paths(team)["gate"].exists()


@pytest.mark.parametrize("has_proof,contradiction,stale_proof", [
    (True, False, False), (False, False, False), (True, True, False), (True, False, True),
])
def test_legacy_empty_day_requires_matching_ledger_and_account(
    workspace, has_proof, contradiction, stale_proof,
):
    team, ledger = workspace
    for name, path in paper_db_paths(team).items():
        conn = _db(path)
        note = f"llm_{name}: 0 fills/0 orders"
        if contradiction and name == "no_gate":
            note = "unknown historical execution"
        conn.execute("INSERT INTO account VALUES (?,100000,100000,?)", (DAY, note))
        conn.commit()
        conn.close()
    plan_hash = canonical_plan_hash(json.loads((team / "outputs" / DAY / "plan.json").read_text()))
    if has_proof:
        proof_hash = "0" * 64 if stale_proof else plan_hash
        old_run = ledger.start_run(
            "daily_finalize", DAY, trigger_source="old-scheduler",
            input_hash=daily_finalize._digest({"plan_hash": proof_hash, "replay": True, "paper": True}),
        )
        step = ledger.start_step(old_run, "paper")
        ledger.finish_step(step, metadata={"result": {
            name: {"status": "succeeded", "orders": 0, "fills": 0}
            for name in ("gate", "no_gate")
        }})
        ledger.finish_run(old_run, status="succeeded", plan_hash=proof_hash)
    if not has_proof or contradiction or stale_proof:
        with pytest.raises(ValueError, match="already settled|legacy evidence"):
            daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
        return
    result = daily_finalize.run_finalize_workflow(DAY, team=team, ledger=ledger, paper=True)
    assert result["status"] == "succeeded"
    for path in paper_db_paths(team).values():
        with sqlite3.connect(path) as conn:
            receipt = conn.execute("SELECT evidence_kind,result_json FROM execution_receipts").fetchone()
            assert receipt[0] == "legacy_ledger_import"
            assert json.loads(receipt[1])["source_run_id"] == old_run
            assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
