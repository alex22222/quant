import json
import sqlite3
from pathlib import Path

import pytest

from test_approvals import PLAN_HASH, env, _read_decision
from trading_team import approvals, db, consistency_check
from trading_team.decision_pipeline import materialize_decision

DAY = "2026-08-23"
REAL_LOG = approvals.log_to_agent_md


def submit(code="600036", decision="approved", request_id="request-one"):
    return approvals.record(DAY, code, code, decision, PLAN_HASH, request_id=request_id)


def wire_consistency(team, monkeypatch):
    for key, value in {"TEAM": team, "DECISIONS_DIR": team / "decisions", "OUTPUTS_DIR": team / "outputs",
                       "APPROVALS_FILE": team / "loops/approvals.json", "TEAM_DB": team / "team.db"}.items():
        monkeypatch.setattr(consistency_check, key, value)


@pytest.mark.parametrize("stage", ["save", "_sync_team_db", "log_to_agent_md"])
def test_projection_failure_preserves_event_and_blocks_execution(env, monkeypatch, stage):
    wire_consistency(env, monkeypatch)
    original = getattr(approvals, stage)
    def fail(*args):
        raise OSError("injected projection failure")
    monkeypatch.setattr(approvals, stage, fail)
    with pytest.raises(approvals.ApprovalPending) as error:
        submit()
    assert error.value.request_id == "request-one"
    doc = _read_decision(env)
    assert doc["approval_events"][0]["event_id"] == "request-one"
    assert doc["approval_projection"]["status"] == "pending"
    assert not consistency_check.check_day(DAY)["ok"]
    before = doc["approval_events"]
    monkeypatch.setattr(approvals, stage, original)
    result = submit()
    assert result["projection_status"] == "complete"
    assert _read_decision(env)["approval_events"] == before
    assert consistency_check.check_day(DAY)["ok"]
    assert len(approvals.load()["decisions"]) == 1


def test_same_id_retries_once_and_different_payload_conflicts(env):
    first = submit()
    second = submit()
    assert first == second
    assert len(_read_decision(env)["approval_events"]) == 1
    with pytest.raises(approvals.ApprovalConflict, match="request"):
        submit(decision="rejected")
    assert len(_read_decision(env)["approval_events"]) == 1


def test_execution_defends_pending_projection_even_if_other_check_is_bypassed(env, monkeypatch):
    from trading_team import execute
    monkeypatch.setattr(approvals, "save", lambda data: (_ for _ in ()).throw(OSError("unavailable")))
    with pytest.raises(approvals.ApprovalPending):
        submit()
    monkeypatch.setattr(execute, "DECISIONS_DIR", env / "decisions")
    monkeypatch.setattr(execute, "DB", env.parent / "paper/paper.db")
    monkeypatch.setattr(execute, "ROOT", env.parent)
    monkeypatch.setattr(execute, "load_execution_bars", lambda *args: pytest.fail("execution reached market data"))
    monkeypatch.setattr(execute.consistency_check, "check_day", lambda day: {"ok": True, "conflicts": [], "warnings": []})
    assert execute.execute(DAY, confirm=True) is False
    assert not execute.DB.exists()


def test_pending_events_survive_workflow_restore_and_recover_once(env, tmp_path, monkeypatch):
    from trading_team.run_ledger import RunLedger
    from trading_team.workflow_recovery import backup_workflow, restore_workflow
    RunLedger(env / ".runs/ledger.db")
    original = approvals.save
    monkeypatch.setattr(approvals, "save", lambda data: (_ for _ in ()).throw(OSError("unavailable")))
    with pytest.raises(approvals.ApprovalPending):
        submit()
    target = tmp_path.parent / (tmp_path.name + "-snapshot")
    result = backup_workflow(team=env, target=target)
    restored = tmp_path.parent / (tmp_path.name + "-restored")
    restore_workflow(target, result["manifest_hash"], restored)
    team = restored / "trading_team"
    for key, value in {"TEAM": team, "FILE": team / "loops/approvals.json", "DECISIONS_DIR": team / "decisions"}.items():
        monkeypatch.setattr(approvals, key, value)
    monkeypatch.setattr(approvals, "save", original)
    assert approvals.status(DAY)["status"] == "pending"
    assert approvals.recover(DAY)["status"] == "complete"
    assert submit()["projection_status"] == "complete"
    assert len(_read_decision(team)["approval_events"]) == 1
    assert _read_decision(env)["approval_projection"]["status"] == "pending"


def test_operational_snapshot_separates_binding_from_approval_sync(env, monkeypatch, finalize_calendar):
    from datetime import datetime
    from trading_team.operational_snapshot import operational_snapshot, SHANGHAI
    monkeypatch.setattr(approvals, "save", lambda data: (_ for _ in ()).throw(OSError("unavailable")))
    with pytest.raises(approvals.ApprovalPending):
        submit()
    result = operational_snapshot(team=env, calendar_path=finalize_calendar, now=datetime(2026, 9, 22, 8, tzinfo=SHANGHAI))
    assert result["research"]["decision"]["status"] == "bound"
    assert result["research"]["approval_audit"]["status"] == "pending"


def test_later_rejection_is_not_undone_by_old_request(env):
    submit()
    submit(decision="rejected", request_id="request-two")
    result = submit()
    assert result["superseded"] is True
    doc = _read_decision(env)
    assert [e["decision"] for e in doc["approval_events"]] == ["approved", "rejected"]
    assert doc["plans"][1]["approval"] == "rejected"
    assert approvals.load()["decisions"][0]["decision"] == "rejected"


def test_plan_replacement_retains_events_but_not_approval(env):
    submit()
    path = env / "outputs" / DAY / "plan.json"
    plan = json.loads(path.read_text())
    plan["market_ok"] = True
    path.write_text(json.dumps(plan))
    old_events = _read_decision(env)["approval_events"]
    rebuilt = materialize_decision(DAY, env)
    assert rebuilt["approval_events"] == old_events
    assert all(p["approval"] == "pending" for p in rebuilt["plans"])
    assert approvals.recover(DAY)["status"] == "complete"
    assert all(p["approval"] == "pending" for p in _read_decision(env)["plans"])
    assert submit()["superseded"] is True


@pytest.mark.parametrize("change", ["decision", "event_hash", "projection", "plan_event"])
def test_corrupted_approval_evidence_is_not_silently_repaired(env, change):
    submit()
    doc = _read_decision(env)
    if change == "decision":
        doc["approval_events"][0]["decision"] = "rejected"
    elif change == "event_hash":
        doc["approval_events"][0]["event_hash"] = "0" * 64
    elif change == "projection":
        doc["approval_projection"]["through_event_hash"] = "0" * 64
    else:
        doc["plans"][1]["approval_event_id"] = "unknown"
    path = env / "decisions" / DAY / "decision.json"
    path.write_text(json.dumps(doc))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="approval"):
        approvals.recover(DAY)
    assert path.read_bytes() == before


def test_database_and_markdown_retry_are_idempotent_after_commit(env, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", env / "team.db")
    db.init_db()
    db.sync_decision(_read_decision(env))
    agent = env.parent / "AGENT.md"
    agent.write_text("# Test\n\n## Loop #1\n- **人工决策**: （待填写）\n")
    monkeypatch.setattr(approvals, "log_to_agent_md", REAL_LOG)
    original = approvals._sync_team_db
    def fail_after_commit(*args):
        original(*args)
        raise RuntimeError("after SQL commit")
    monkeypatch.setattr(approvals, "_sync_team_db", fail_after_commit)
    with pytest.raises(approvals.ApprovalPending):
        submit()
    monkeypatch.setattr(approvals, "_sync_team_db", original)
    submit()
    approvals.recover(DAY)
    with sqlite3.connect(env / "team.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals WHERE event_id='request-one'").fetchone()[0] == 1
        assert conn.execute("SELECT approval FROM plans WHERE code='600036'").fetchone()[0] == "approved"
    assert agent.read_text().count("approval-event:request-one") == 1


def test_pending_status_is_read_only_and_recovery_does_not_create_new_intent(env, monkeypatch):
    original = approvals.save
    monkeypatch.setattr(approvals, "save", lambda data: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(approvals.ApprovalPending):
        submit()
    files = {p: p.read_bytes() for p in env.rglob("*") if p.is_file()}
    assert approvals.status(DAY)["status"] == "pending"
    assert files == {p: p.read_bytes() for p in env.rglob("*") if p.is_file()}
    monkeypatch.setattr(approvals, "save", original)
    assert approvals.recover(DAY)["status"] == "complete"
    assert len(_read_decision(env)["approval_events"]) == 1


def test_missing_projection_detected_even_after_completed_marker(env):
    submit()
    (env / "loops/approvals.json").unlink()
    assert approvals.status(DAY)["status"] == "pending"
    assert approvals.recover(DAY)["status"] == "complete"
    assert len(approvals.load()["decisions"]) == 1


def test_failure_before_primary_commit_has_no_event(env, monkeypatch):
    from trading_team import state_lock
    before = _read_decision(env)
    monkeypatch.setattr(state_lock.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("replace refused")))
    with pytest.raises(OSError):
        submit()
    assert _read_decision(env) == before
    assert not (env / "loops/approvals.json").exists()


@pytest.mark.parametrize("stage", ["primary_fsync", "acknowledgement", "markdown_after_write"])
def test_crash_windows_recover_without_new_event(env, monkeypatch, stage):
    original = approvals.atomic_write_json
    writes = 0
    agent = env.parent / "AGENT.md"
    agent.write_text("# Test\n\n## Loop #1\n- **人工决策**: （待填写）\n")
    monkeypatch.setattr(approvals, "log_to_agent_md", REAL_LOG)
    def write(path, value):
        nonlocal writes
        if path.name == "decision.json":
            writes += 1
            if stage == "acknowledgement" and writes == 2:
                raise OSError("acknowledgement failed")
        original(path, value)
        if stage == "primary_fsync" and path.name == "decision.json" and writes == 1:
            raise OSError("fsync failed after replacement")
    def log(entry):
        REAL_LOG(entry)
        raise RuntimeError("crash after markdown write")
    monkeypatch.setattr(approvals, "atomic_write_json", write)
    if stage == "markdown_after_write":
        monkeypatch.setattr(approvals, "log_to_agent_md", log)
    with pytest.raises(approvals.ApprovalPending):
        submit()
    monkeypatch.setattr(approvals, "atomic_write_json", original)
    monkeypatch.setattr(approvals, "log_to_agent_md", REAL_LOG)
    assert submit()["projection_status"] == "complete"
    assert len(_read_decision(env)["approval_events"]) == 1
    assert agent.read_text().count("approval-event:request-one") == 1


@pytest.mark.parametrize("destination", ["team.db", "AGENT.md"])
def test_missing_previously_synced_destination_is_not_downgraded_to_optional(env, monkeypatch, destination):
    monkeypatch.setattr(db, "DB_PATH", env / "team.db")
    db.init_db()
    agent = env.parent / "AGENT.md"
    agent.write_text("# Test\n\n## Loop #1\n- **人工决策**: （待填写）\n")
    monkeypatch.setattr(approvals, "log_to_agent_md", REAL_LOG)
    submit()
    target = env / "team.db" if destination == "team.db" else agent
    target.unlink()
    assert approvals.status(DAY)["status"] == "pending"
    with pytest.raises(approvals.ApprovalPending, match="missing"):
        approvals.recover(DAY)
    assert approvals.status(DAY)["status"] == "pending"


def test_materializing_new_version_retains_required_projection_destinations(env, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", env / "team.db")
    db.init_db()
    submit()
    (env / "team.db").unlink()
    path = env / "outputs" / DAY / "plan.json"
    plan = json.loads(path.read_text())
    plan["market_ok"] = True
    path.write_text(json.dumps(plan))
    materialize_decision(DAY, env)
    with pytest.raises(approvals.ApprovalPending, match="missing"):
        approvals.recover(DAY)
    assert approvals.status(DAY)["status"] == "pending"


def test_unwritable_required_human_template_remains_pending(env, monkeypatch):
    agent = env.parent / "AGENT.md"
    template = "# Test\n\n## Loop #1\n- **人工决策**: （待填写）\n"
    agent.write_text(template)
    monkeypatch.setattr(approvals, "log_to_agent_md", REAL_LOG)
    submit()
    agent.write_text("# Existing file without an approval insertion point\n")
    assert approvals.status(DAY)["status"] == "pending"
    with pytest.raises(approvals.ApprovalPending, match="human audit"):
        approvals.recover(DAY)
    assert approvals.status(DAY)["status"] == "pending"
    assert _read_decision(env)["approval_projection"]["destinations"]["agent_md"] == "synced"
    agent.write_text(template)
    assert approvals.recover(DAY)["status"] == "complete"
    assert len(_read_decision(env)["approval_events"]) == 1
    assert agent.read_text().count("approval-event:request-one") == 1


def test_status_detects_new_plan_before_materialization(env):
    submit()
    path = env / "outputs" / DAY / "plan.json"
    plan = json.loads(path.read_text())
    plan["market_ok"] = True
    path.write_text(json.dumps(plan))
    assert approvals.status(DAY)["status"] == "invalid"


def test_invalid_projection_destinations_refused(env):
    submit()
    path = env / "decisions" / DAY / "decision.json"
    doc = _read_decision(env)
    doc["approval_projection"]["destinations"] = []
    path.write_text(json.dumps(doc))
    assert approvals.status(DAY)["status"] == "invalid"
    with pytest.raises(ValueError, match="approval"):
        approvals.recover(DAY)


def console_handler(monkeypatch, body=None, path="/api/approve"):
    import importlib.util
    from io import BytesIO
    spec = importlib.util.spec_from_file_location("approval_recovery_console", Path(__file__).resolve().parents[1] / "console/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", approvals.TEAM.parent)
    handler = object.__new__(module.H)
    handler.path = path
    payload = json.dumps(body or {}).encode()
    handler.headers = {"Content-Length": str(len(payload))}
    handler.rfile = BytesIO(payload)
    sent = []
    handler._send_json = lambda status, value: sent.append((status, value))
    handler.send_error = lambda status: sent.append((status, {}))
    return handler, sent


def test_api_distinguishes_recorded_pending_and_explicit_recovery(env, monkeypatch):
    original = approvals.save
    monkeypatch.setattr(approvals, "save", lambda doc: (_ for _ in ()).throw(OSError("unavailable")))
    handler, sent = console_handler(monkeypatch, {
        "date": DAY, "code": "600036", "name": "600036", "decision": "approved",
        "plan_hash": PLAN_HASH, "request_id": "api-request",
    })
    handler.do_POST()
    assert sent[0][0] == 503
    assert sent[0][1]["code"] == "approval_projection_pending"
    assert sent[0][1]["recorded_event_id"] == "api-request"
    handler, sent = console_handler(monkeypatch, path=f"/api/approvals?date={DAY}")
    handler.do_GET()
    assert sent[0][1]["approval"]["status"] == "pending"
    monkeypatch.setattr(approvals, "save", original)
    handler, sent = console_handler(monkeypatch, {"date": DAY}, "/api/approvals/recover")
    handler.do_POST()
    assert sent[0][0] == 200
    assert sent[0][1]["approval"]["status"] == "complete"
    assert len(_read_decision(env)["approval_events"]) == 1


@pytest.mark.parametrize("after_approval", [False, True])
@pytest.mark.parametrize("change", ["entry", "position", "market"])
def test_copied_hash_cannot_authorize_changed_decision_facts(env, monkeypatch, change, after_approval):
    if after_approval:
        submit(code="601318")
    path = env / "decisions" / DAY / "decision.json"
    doc = _read_decision(env)
    if change == "entry":
        doc["plans"][0]["entry"] = [80]
    elif change == "position":
        doc["plans"][0]["position_pct"] = .5
    else:
        doc["market_ok"] = True
    path.write_text(json.dumps(doc))
    before = path.read_bytes()
    if after_approval:
        wire_consistency(env, monkeypatch)
        assert approvals.status(DAY)["status"] == "invalid"
        assert not consistency_check.check_day(DAY)["ok"]
        with pytest.raises(ValueError, match="facts"):
            approvals.recover(DAY)
    else:
        with pytest.raises(ValueError, match="facts"):
            submit(code="601318")
    assert path.read_bytes() == before
