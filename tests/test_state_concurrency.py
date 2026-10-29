import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from test_approvals import PLAN_HASH, env
from test_workflow_recovery import workspace
from trading_team import approvals


def test_two_approvals_do_not_lose_the_first_log(env, monkeypatch):
    entered, release = Event(), Event()
    original = approvals.load
    def paused_load():
        result = original()
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(approvals, "load", paused_load)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(approvals.record, "2026-08-23", "601318", "A", "approved", PLAN_HASH)
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match="busy"):
                approvals.record("2026-08-23", "600036", "B", "rejected", PLAN_HASH)
        finally:
            release.set()
        assert first.result()["master_synced"]
    approvals.record("2026-08-23", "600036", "B", "rejected", PLAN_HASH)
    assert {row["code"] for row in approvals.load()["decisions"]} == {"601318", "600036"}


def test_two_notifications_cannot_call_sender_concurrently(tmp_path):
    from trading_team.notification_outbox import push_once
    entered, release = Event(), Event()
    calls = []
    def sender(day):
        calls.append(day)
        entered.set()
        assert release.wait(5)
        return {"pushed": True}
    team = tmp_path / "trading_team"
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(push_once, "2026-09-21", "hash", team=team, sender=sender)
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match="busy"):
                push_once("2026-09-21", "hash", team=team, sender=lambda day: {"pushed": True})
        finally:
            release.set()
        assert first.result()["status"] == "sent"
    assert push_once("2026-09-21", "hash", team=team, sender=sender)["status"] == "skipped"
    assert calls == ["2026-09-21"]


def test_state_lock_is_reentrant_but_not_shared_between_threads(tmp_path):
    from trading_team.state_lock import state_lock
    team = tmp_path / "trading_team"
    with state_lock(team), state_lock(team):
        with ThreadPoolExecutor(max_workers=1) as pool:
            def other():
                with pytest.raises(RuntimeError, match="busy"), state_lock(team):
                    pytest.fail("different thread entered")
            pool.submit(other).result()
    with state_lock(team):
        pass


def test_state_lock_releases_after_failure_and_different_roots_are_independent(tmp_path):
    from trading_team.state_lock import state_lock
    with pytest.raises(ValueError), state_lock(tmp_path / "one"):
        with state_lock(tmp_path / "two"):
            raise ValueError("failure")
    with state_lock(tmp_path / "one"):
        pass


def test_lock_cannot_follow_symlink(tmp_path):
    from trading_team.state_lock import state_lock
    team = tmp_path / "trading_team"
    locks = team / ".runs/locks"
    locks.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("do not modify")
    (locks / "shared-state.lock").symlink_to(outside)
    with pytest.raises((OSError, ValueError)), state_lock(team):
        pytest.fail("followed symlink")
    assert outside.read_text() == "do not modify"


def test_atomic_json_failure_keeps_old_file_and_cleans_temporary(tmp_path, monkeypatch):
    from trading_team.state_lock import atomic_write_json
    target = tmp_path / "state.json"
    target.write_text('{"old":true}')
    def fail(*args):
        raise OSError("injected replace failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="injected"):
        atomic_write_json(target, {"new": True})
    assert json.loads(target.read_text()) == {"old": True}
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("operation", ["live_decision", "paper_decision", "paper", "finalize", "index", "accuracy", "enrich", "proposal_backfill",
                                       "valuation", "legacy_receipts", "legacy_execute", "notification", "counterfactual", "publication", "postprocess", "materialize_day"])
def test_all_shared_entrypoints_refuse_before_read_or_write(tmp_path, monkeypatch, operation):
    from trading_team.state_lock import state_lock
    from trading_team.build_index import build_index
    from trading_team.conclusion_block import backfill_accuracy, enrich_plan
    from trading_team.daily_finalize import materialize_day, run_finalize_workflow
    from trading_team.decision_pipeline import materialize_decision
    from trading_team.paper_pipeline import import_legacy_empty_receipts, materialize_paper_decision, record_current_valuations, run_paper_day
    from trading_team.paper_plans import ensure_paper_proposals
    from trading_team import execute
    from trading_team.counterfactual import write_counterfactual
    from trading_team.notification_outbox import push_once
    from trading_team.kimi_daily import DailyMeetingRunner
    team = tmp_path / "trading_team"
    runner = object.__new__(DailyMeetingRunner)
    runner.team = team
    monkeypatch.setattr(execute, "DECISIONS_DIR", team / "decisions")
    calls = {
        "live_decision": lambda: materialize_decision("2026-09-21", team),
        "paper_decision": lambda: materialize_paper_decision("2026-09-21", team),
        "paper": lambda: run_paper_day("2026-09-21", team),
        "finalize": lambda: run_finalize_workflow("2026-09-21", team=team),
        "index": lambda: build_index(team),
        "accuracy": lambda: backfill_accuracy("2026-09-21", team),
        "enrich": lambda: enrich_plan("2026-09-21", team),
        "proposal_backfill": lambda: ensure_paper_proposals("2026-09-21", team),
        "valuation": lambda: record_current_valuations(team=team),
        "legacy_receipts": lambda: import_legacy_empty_receipts("2026-09-21", "hash", {}, "run", team),
        "legacy_execute": lambda: execute.execute("2026-09-21", confirm=True),
        "notification": lambda: push_once("2026-09-21", "hash", team=team, sender=lambda day: pytest.fail("sent")),
        "counterfactual": lambda: write_counterfactual("2026-09-21", team),
        "publication": lambda: runner._publish_generation("run", "2026-09-21", team / "stage", {}, {}),
        "postprocess": lambda: runner._postprocess("run", "2026-09-21", True),
        "materialize_day": lambda: materialize_day("2026-09-21", team),
    }
    with state_lock(team):
        with ThreadPoolExecutor(max_workers=1) as pool:
            def invoke():
                with pytest.raises(RuntimeError, match="busy"):
                    calls[operation]()
            pool.submit(invoke).result()
    assert not (team / "outputs").exists()
    assert not (team / "decisions").exists()
    assert not (team / ".runs/ledger.db").exists()
    assert not (tmp_path / "paper").exists()


@pytest.mark.parametrize("change", ["new_plan", "missing_plan"])
def test_approval_checks_current_plan_before_any_write(env, change):
    plan_path = env / "outputs/2026-08-23/plan.json"
    decision_path = env / "decisions/2026-08-23/decision.json"
    before = decision_path.read_bytes()
    if change == "new_plan":
        plan = json.loads(plan_path.read_text())
        plan["market_ok"] = True
        plan_path.write_text(json.dumps(plan))
    else:
        plan_path.unlink()
    with pytest.raises(ValueError, match="plan"):
        approvals.record("2026-08-23", "601318", "A", "approved", PLAN_HASH)
    assert decision_path.read_bytes() == before
    assert approvals.load()["decisions"] == []


def test_stale_in_memory_decision_cannot_overwrite_another_approval(env):
    path = env / "decisions/2026-08-23/decision.json"
    stale = json.loads(path.read_text())
    approvals.record("2026-08-23", "601318", "A", "approved", PLAN_HASH)
    approvals.write_back_to_decision("2026-08-23", "600036", "rejected", PLAN_HASH,
                                    "", "2026-08-23T10:00:00", stale)
    assert [p["approval"] for p in json.loads(path.read_text())["plans"]] == ["approved", "rejected"]


def test_approval_api_returns_retryable_conflict(monkeypatch):
    import importlib.util
    from io import BytesIO
    from trading_team.state_lock import StateBusy
    spec = importlib.util.spec_from_file_location("concurrency_console_test", Path(__file__).resolve().parents[1] / "console/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def busy(**kwargs):
        raise StateBusy("shared state busy")
    monkeypatch.setattr(approvals, "record", busy)
    handler = object.__new__(module.H)
    handler.path = "/api/approve"
    body = json.dumps({"date": "2026-09-21", "code": "600036", "decision": "approved", "plan_hash": PLAN_HASH}).encode()
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    sent = []
    handler._send_json = lambda status, value: sent.append((status, value))
    handler.do_POST()
    assert sent == [(409, {"ok": False, "code": "state_busy", "retryable": True, "error": "shared state busy"})]


def test_finalize_cannot_take_over_an_active_attempt(tmp_path, monkeypatch, finalize_calendar):
    from test_daily_finalize_workflow import _write_plan
    from trading_team import daily_finalize
    from trading_team.run_ledger import RunLedger
    team = tmp_path / "trading_team"
    _write_plan(team)
    ledger = RunLedger(team / ".runs/ledger.db")
    entered, release = Event(), Event()
    original = daily_finalize.materialize_day
    def paused(day, team):
        entered.set()
        assert release.wait(5)
        return original(day, team)
    monkeypatch.setattr(daily_finalize, "materialize_day", paused)
    kwargs = dict(team=team, ledger=ledger, replay=False, paper=False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(daily_finalize.run_finalize_workflow, "2026-09-21", **kwargs)
        try:
            assert entered.wait(5)
            before = ledger.get_run(ledger.list_runs()[0]["run_id"])
            for day in ("2026-09-21", "2026-09-22"):
                with pytest.raises(RuntimeError, match="busy"):
                    daily_finalize.run_finalize_workflow(day, **kwargs)
            assert ledger.get_run(before["run_id"]) == before
            assert len(ledger.list_runs()) == 1
        finally:
            release.set()
        result = first.result()
    assert result["status"] == "succeeded"
    assert daily_finalize.run_finalize_workflow("2026-09-21", **kwargs)["status"] == "reused"
    assert ledger.get_run(result["run_id"])["attempt"] == 1


def test_cross_date_accuracy_updates_are_preserved(tmp_path, monkeypatch):
    from trading_team import conclusion_block
    team = tmp_path / "trading_team"
    (team / "loops").mkdir(parents=True)
    path = team / "loops/accuracy.json"
    path.write_text('{"entries":[]}')
    for day in ("2026-09-18", "2026-09-21"):
        report = team / "outputs" / day / "05_researcher.md"
        report.parent.mkdir(parents=True)
        report.write_text('```json\n' + json.dumps({"role": "researcher", "date": day, "ratings": [
            {"code": "600036", "rating": "中性", "confidence": 3}
        ]}) + '\n```')
    entered, release = Event(), Event()
    original = conclusion_block._ref_price
    def paused(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(conclusion_block, "_ref_price", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(conclusion_block.backfill_accuracy, "2026-09-18", team)
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match="busy"):
                conclusion_block.backfill_accuracy("2026-09-21", team)
        finally:
            release.set()
        assert first.result()["updated"]
    assert conclusion_block.backfill_accuracy("2026-09-21", team)["updated"]
    assert {row["date"] for row in json.loads(path.read_text())["entries"]} == {"2026-09-18", "2026-09-21"}


def test_process_crash_releases_lock(tmp_path):
    import subprocess
    import sys
    from trading_team.state_lock import StateBusy, state_lock
    team = tmp_path / "trading_team"
    code = """import sys
from pathlib import Path
from trading_team.state_lock import state_lock
with state_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-u", "-c", code, str(team)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(StateBusy), state_lock(team):
            pytest.fail("another process entered")
    finally:
        process.kill()
        process.communicate(timeout=5)
    with state_lock(team):
        pass


def test_atomic_replacement_exposes_only_complete_documents(tmp_path, monkeypatch):
    from trading_team.state_lock import atomic_write_json
    path = tmp_path / "state.json"
    path.write_text('{"old":true}')
    original = os.replace
    def inspect(source, target):
        assert json.loads(path.read_text()) == {"old": True}
        assert json.loads(Path(source).read_text()) == {"new": list(range(200))}
        return original(source, target)
    monkeypatch.setattr(os, "replace", inspect)
    atomic_write_json(path, {"new": list(range(200))})
    assert json.loads(path.read_text()) == {"new": list(range(200))}


def test_backup_excludes_first_writer_without_existing_lock_file(workspace, tmp_path, monkeypatch):
    from trading_team import workflow_recovery
    from trading_team.build_index import build_index
    entered, release = Event(), Event()
    original = workflow_recovery._inventory
    def paused(root):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(root)
    monkeypatch.setattr(workflow_recovery, "_inventory", paused)
    assert not (workspace / ".runs/locks/shared-state.lock").exists()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(workflow_recovery.backup_workflow, team=workspace, target=tmp_path / "backup")
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match="busy"):
                build_index(workspace)
        finally:
            release.set()
        result = first.result()
    workflow_recovery.verify_workflow_backup(tmp_path / "backup", result["manifest_hash"])


def test_collector_refuses_duplicate_date_before_loading_state(tmp_path):
    from trading_team.collect import collect_day
    from trading_team.state_lock import state_lock
    team = tmp_path / "trading_team"
    with state_lock(team, "data-collect-2026-09-21"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            def invoke():
                with pytest.raises(RuntimeError, match="busy"):
                    collect_day("2026-09-21", root=team)
            pool.submit(invoke).result()
    assert not (team / "context").exists()
    assert not (team / ".runs/ledger.db").exists()
