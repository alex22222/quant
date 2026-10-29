import hashlib
import gc
import json
import sqlite3
from contextlib import closing

import pytest

from test_daily_research_recovery import DAY, ScriptedClient, meeting
from trading_team.paper_pipeline import _db, paper_db_paths
from trading_team.run_ledger import RunLedger


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "source"
    team = root / "trading_team"
    ledger = RunLedger(team / ".runs/ledger.db")
    run = ledger.start_run("daily_research", "2026-09-21", trigger_source="offline", model="fixture")
    step = ledger.start_step(run, "role_03")
    ledger.finish_step(step, status="failed", error="interrupted fixture")
    ledger.finish_run(run, status="failed", error="interrupted fixture")
    files = {
        "trading_team/watchlist.json": '{"stocks":[]}',
        "trading_team/context/2026-09-21/manifest.json": '{"ok":false}',
        "trading_team/outputs/2026-09-21/plan.json": '{"date":"2026-09-21","proposals":[]}',
        "trading_team/decisions/2026-09-21/decision.json": '{"pm_status":"pending"}',
        "trading_team/prompts/07_risk_manager.md": "offline charter",
        "trading_team/loops/approvals.json": '{"decisions":[]}',
        "trading_team/.runs/staging/2026-09-21/01_fundamental.input.txt": "offline input",
        "trading_team/.runs/staging/2026-09-21/01_fundamental.run.json": '{"output_hash":"fixture"}',
        "trading_team/.runs/rejected/run/3.json": '{"raw":"bad review"}',
        "trading_team/.runs/outbox/feishu/date-hash.json": '{"status":"sent"}',
        "multica/workspace.json": '{"schema_version":1}',
        "multica/runbooks/daily-research.md": "local runbook",
        "docs/MULTICA_PRODUCTION_RUNBOOK.md": "production publication contract",
        "AGENT.md": "local approval audit",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for path in paper_db_paths(team).values():
        _db(path).close()
    with sqlite3.connect(team / "team.db") as conn:
        conn.execute("CREATE TABLE facts (key TEXT PRIMARY KEY,value BLOB) WITHOUT ROWID")
        conn.execute("INSERT INTO facts VALUES ('key',?)", (b"evidence",))
    for relative in (".env", "trading_team/.env", "trading_team/.runs/backups/old/manifest.json",
                     "multica/credentials.json", "trading_team/.runs/secret-token.json"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DO NOT EXPORT")
    return team


def backup(team, target):
    from trading_team.workflow_recovery import backup_workflow
    return backup_workflow(team=team, target=target)


def test_roundtrip_preserves_evidence_and_failed_run_without_secrets(workspace, tmp_path):
    from trading_team.workflow_recovery import restore_workflow, verify_workflow_backup
    # Settle fixture writers before byte sampling; their eventual close can checkpoint WAL.
    gc.collect()
    before = {str(p.relative_to(workspace.parent)): p.read_bytes()
              for p in workspace.parent.rglob("*") if p.is_file() and not p.name.endswith(("-wal", "-shm"))}
    target = tmp_path / "backup"
    result = backup(workspace, target)
    manifest = verify_workflow_backup(target, result["manifest_hash"])
    assert manifest["scope"] == "research_paper_workflow_evidence"
    assert "docs/MULTICA_PRODUCTION_RUNBOOK.md" in manifest["files"]
    assert manifest["execution_authorized"] is False
    assert manifest["missing_databases"] == []
    assert manifest["databases"]["trading_team/.runs/ledger.db"]["tables"]["workflow_attempts"]["count"] == 1
    restored = tmp_path / "restored"
    response = restore_workflow(target, result["manifest_hash"], restored)
    assert response["status"] == "verified_isolated"
    assert response["execution_authorized"] is False
    assert not list(restored.rglob("*.py")) and not list(restored.rglob(".env"))
    assert not (restored / "multica/credentials.json").exists()
    assert not (restored / "trading_team/.runs/secret-token.json").exists()
    assert not (restored / "trading_team/.runs/backups").exists()
    runs = RunLedger(restored / "trading_team/.runs/ledger.db", readonly=True).list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    for relative, content in before.items():
        assert (workspace.parent / relative).read_bytes() == content
        if relative in manifest["files"]:
            assert (restored / relative).read_bytes() == content


def test_audit_artifacts_survive_restore_without_code_or_environment(workspace, tmp_path):
    from trading_team.workflow_recovery import restore_workflow, verify_workflow_backup
    root = workspace.parent
    artifacts = {
        "docs/audits/maker-result.json": b'{"production_publication":false}',
        "docs/audits/reviews/checker.json": b'{"verdict":"accept","scope":"isolated"}',
    }
    excluded = ("docs/audits/.env", "docs/audits/.tokens.json",
                "docs/audits/helper.py", "docs/unrelated.json")
    for relative, content in artifacts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for relative in excluded:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DO NOT EXPORT")
    result = backup(workspace, tmp_path / "audit-backup")
    manifest = verify_workflow_backup(tmp_path / "audit-backup", result["manifest_hash"])
    for relative in artifacts:
        assert relative in manifest["files"]
    restored = tmp_path / "audit-restored"
    response = restore_workflow(tmp_path / "audit-backup", result["manifest_hash"], restored)
    assert response["execution_authorized"] is False
    for relative, content in artifacts.items():
        assert (restored / relative).read_bytes() == content
        assert (root / relative).read_bytes() == content
    for relative in excluded:
        assert relative not in manifest["files"]
        assert not (restored / relative).exists()


def test_ledger_committed_wal_survives_and_uncommitted_rows_do_not(workspace, tmp_path):
    path = workspace / ".runs/ledger.db"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE workflow_runs SET error='committed WAL'")
        writer.commit()
        writer.execute("UPDATE workflow_runs SET error='uncommitted'")
        result = backup(workspace, tmp_path / "backup")
        with sqlite3.connect(tmp_path / "backup/trading_team/.runs/ledger.db") as conn:
            assert conn.execute("SELECT error FROM workflow_runs").fetchone()[0] == "committed WAL"
        assert result["status"] == "verified"


@pytest.mark.parametrize("relative", ["manifest.json", "trading_team/watchlist.json",
    "trading_team/.runs/ledger.db", "trading_team/.runs/outbox/feishu/date-hash.json", "paper/llm_gate.db"])
def test_tampering_never_creates_restore_target(workspace, tmp_path, relative):
    from trading_team.workflow_recovery import restore_workflow
    target = tmp_path / "backup"
    result = backup(workspace, target)
    (target / relative).write_bytes(b"changed")
    with pytest.raises(ValueError):
        restore_workflow(target, result["manifest_hash"], tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("change", ["file", "new_file", "remove_file", "database", "database_replace"])
def test_concurrent_source_change_never_publishes_manifest(workspace, tmp_path, monkeypatch, change):
    from trading_team import workflow_recovery
    original = workflow_recovery._copy_database
    changed = False
    def copy(source, target):
        nonlocal changed
        original(source, target)
        if changed:
            return
        changed = True
        if change == "file":
            (workspace / "watchlist.json").write_text('{"stocks":["new"]}')
        elif change == "new_file":
            (workspace / "outputs/2026-09-21/new.json").write_text("{}")
        elif change == "remove_file":
            (workspace / "loops/approvals.json").unlink()
        elif change == "database":
            with sqlite3.connect(workspace / ".runs/ledger.db") as conn:
                conn.execute("UPDATE workflow_runs SET error='concurrent change'")
        else:
            path = workspace / "team.db"
            other = path.with_suffix(".replacement")
            other.write_bytes(path.read_bytes())
            other.replace(path)
    monkeypatch.setattr(workflow_recovery, "_copy_database", copy)
    with pytest.raises(ValueError, match="changed"):
        backup(workspace, tmp_path / "backup")
    assert not (tmp_path / "backup/manifest.json").exists()


def test_source_symlink_is_not_followed(workspace, tmp_path):
    source = workspace / "outputs/2026-09-21/plan.json"
    source.unlink()
    source.symlink_to(workspace.parent / ".env")
    with pytest.raises(ValueError, match="symlink"):
        backup(workspace, tmp_path / "backup")
    assert not (tmp_path / "backup/manifest.json").exists()


@pytest.mark.parametrize("relative", ["../../escaped.json", "/tmp/escaped.json", "trading_team/../escaped.json",
                                       ".env", "multica/credentials.json"])
def test_manifest_paths_cannot_restore_outside_contract(workspace, tmp_path, relative):
    from trading_team.workflow_recovery import restore_workflow
    target = tmp_path / "backup"
    backup(workspace, target)
    path = target / "manifest.json"
    doc = json.loads(path.read_bytes())
    doc["files"][relative] = next(iter(doc["files"].values()))
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        restore_workflow(target, hashlib.sha256(path.read_bytes()).hexdigest(), tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_missing_accounts_remain_missing_not_initialized(workspace, tmp_path):
    from trading_team.workflow_recovery import restore_workflow, verify_workflow_backup
    for path in paper_db_paths(workspace).values():
        path.unlink()
    target = tmp_path / "backup"
    result = backup(workspace, target)
    manifest = verify_workflow_backup(target, result["manifest_hash"])
    assert set(manifest["missing_databases"]) == {"paper/llm_gate.db", "paper/llm_no_gate.db"}
    restore_workflow(target, result["manifest_hash"], tmp_path / "restored")
    assert not (tmp_path / "restored/paper").exists()


def test_existing_and_source_destinations_are_refused(workspace, tmp_path):
    from trading_team.workflow_recovery import restore_workflow
    with pytest.raises(ValueError, match="destination"):
        backup(workspace, workspace / ".runs/backups/new")
    target = tmp_path / "backup"
    result = backup(workspace, target)
    for destination in (target, target / "new", workspace.parent, workspace.parent / "new", tmp_path):
        with pytest.raises(ValueError, match="destination"):
            restore_workflow(target, result["manifest_hash"], destination)


def test_account_writer_lock_blocks_snapshot(workspace, tmp_path):
    from trading_team.paper_lock import account_locks
    with account_locks(paper_db_paths(workspace).values()):
        with pytest.raises(RuntimeError, match="busy"):
            backup(workspace, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_failed_copy_retains_partial_without_completion_marker(workspace, tmp_path, monkeypatch):
    from trading_team import workflow_recovery
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(workflow_recovery, "_copy_database", fail)
    with pytest.raises(OSError, match="disk full"):
        backup(workspace, tmp_path / "backup")
    assert (tmp_path / "backup").exists()
    assert not (tmp_path / "backup/manifest.json").exists()


@pytest.mark.parametrize("completed", [True, False])
def test_restored_research_verifies_publication_or_resumes_failed_role(meeting, tmp_path, monkeypatch, completed):
    from trading_team.daily_finalize import materialize_day
    from trading_team.kimi_daily import DailyMeetingRunner, inspect_generation
    from trading_team.workflow_recovery import restore_workflow
    runner, client, _ = meeting
    if completed:
        runner.run(DAY, collect=False, push=False)
    else:
        client.fail_role = "technical"
        with pytest.raises(RuntimeError, match="interruption"):
            runner.run(DAY, collect=False, push=False)
    snapshot = tmp_path.with_name(tmp_path.name + "-snapshot")
    result = backup(runner.team, snapshot)
    destination = tmp_path.with_name(tmp_path.name + "-restored")
    restore_workflow(snapshot, result["manifest_hash"], destination)
    team = destination / "trading_team"
    restored_client = ScriptedClient()
    restored = DailyMeetingRunner(restored_client, team=team)
    monkeypatch.setattr(restored, "_postprocess", lambda *_args: materialize_day(DAY, team))
    assert restored.run(DAY, collect=False, push=False)["status"] == "succeeded"
    assert inspect_generation(team, DAY, restored.ledger)["plan_hash"]
    if completed:
        assert not restored_client.calls
    else:
        assert set(restored_client.calls) == {"technical", "researcher", "trader", "risk_manager"}
        run = restored.ledger.list_runs()[0]
        assert [a["status"] for a in restored.ledger.get_run(run["run_id"])["attempts"]] == ["failed", "succeeded"]


def test_restored_settled_paper_reuses_without_repeating_orders(workspace, tmp_path, execution_cache, finalize_calendar, monkeypatch, stub_research_reviews):
    from trading_team.daily_finalize import run_finalize_workflow
    from trading_team.risk_contract import build_reviewed_plan
    from trading_team.workflow_recovery import restore_workflow
    candidate = {"code": "600036", "direction": "买入", "position_pct": .1,
                 "entry": [40], "entry_type": "limit", "stop": 38}
    plan = build_reviewed_plan(DAY, "2026-09-18", True, [candidate],
                               [{"code": "600036", "decision": "通过", "reason_codes": []}])
    (workspace / "outputs" / DAY / "plan.json").write_text(json.dumps(plan))
    execution_cache(workspace, DAY, {code: {"open": 40, "high": 41, "low": 39, "close": 40, "prev_close": 40}
                                    for code in ("600036", "000300")})
    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: pytest.fail("network"))
    first = run_finalize_workflow(DAY, team=workspace, paper=True, calendar_path=finalize_calendar)
    assert first["status"] == "succeeded"
    result = backup(workspace, tmp_path / "snapshot")
    restore_workflow(tmp_path / "snapshot", result["manifest_hash"], tmp_path / "restored")
    team = tmp_path / "restored/trading_team"
    before = {name: path.read_bytes() for name, path in paper_db_paths(team).items()}
    assert run_finalize_workflow(DAY, team=team, paper=True, calendar_path=finalize_calendar)["status"] == "reused"
    assert before == {name: path.read_bytes() for name, path in paper_db_paths(team).items()}
    for path in paper_db_paths(team).values():
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_restored_sent_notification_is_not_sent_again(workspace, tmp_path):
    from trading_team.notification_outbox import push_once
    from trading_team.workflow_recovery import restore_workflow
    result = backup(workspace, tmp_path / "snapshot")
    restore_workflow(tmp_path / "snapshot", result["manifest_hash"], tmp_path / "restored")
    response = push_once("date", "hash", team=tmp_path / "restored/trading_team", sender=lambda *_: pytest.fail("send"))
    assert response["status"] == "skipped"


def test_restored_ambiguous_notification_requires_resolution(workspace, tmp_path):
    from trading_team.notification_outbox import push_once, status, resolve, DeliveryUnknown
    from trading_team.workflow_recovery import restore_workflow
    with pytest.raises(DeliveryUnknown):
        push_once(DAY, "uncertain-plan", team=workspace, sender=lambda _: {"pushed": False})
    source = status(DAY, "uncertain-plan", team=workspace)
    result = backup(workspace, tmp_path / "snapshot")
    restore_workflow(tmp_path / "snapshot", result["manifest_hash"], tmp_path / "restored")
    team = tmp_path / "restored/trading_team"
    assert status(DAY, "uncertain-plan", team=team) == source
    with pytest.raises(DeliveryUnknown):
        push_once(DAY, "uncertain-plan", team=team, sender=lambda _: pytest.fail("duplicate"))
    resolve(DAY, "uncertain-plan", team=team, expected_hash=source["record_hash"], attempt_id=source["attempt_id"],
            outcome="delivered", operator="offline", evidence="isolated provider log", message_id="om_restored")
    assert push_once(DAY, "uncertain-plan", team=team, sender=lambda _: pytest.fail("duplicate"))["status"] == "skipped"
    assert status(DAY, "uncertain-plan", team=workspace) == source


def test_relative_team_path_with_existing_operation_locks(workspace, tmp_path, monkeypatch):
    lock = workspace / ".runs/locks/daily_research-2026-09-21.lock"
    lock.parent.mkdir(parents=True)
    lock.touch()
    monkeypatch.chdir(workspace.parent)
    from pathlib import Path
    assert backup(Path("trading_team"), tmp_path / "snapshot")["status"] == "verified"


@pytest.mark.parametrize("change", ["missing_source", "relative_source", "files_list", "missing_list", "authorized"])
def test_malformed_manifest_fails_before_restore(workspace, tmp_path, change):
    from trading_team.workflow_recovery import restore_workflow
    target = tmp_path / "snapshot"
    backup(workspace, target)
    path = target / "manifest.json"
    doc = json.loads(path.read_bytes())
    if change == "missing_source":
        doc.pop("source_root")
    elif change == "relative_source":
        doc["source_root"] = "relative"
    elif change == "files_list":
        doc["files"] = []
    elif change == "missing_list":
        doc["missing_databases"] = [None]
    else:
        doc["execution_authorized"] = True
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        restore_workflow(target, hashlib.sha256(path.read_bytes()).hexdigest(), tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_live_operation_lock_is_respected_but_stale_running_state_is_preserved(workspace, tmp_path):
    import fcntl
    from trading_team.workflow_recovery import restore_workflow
    ledger = RunLedger(workspace / ".runs/ledger.db")
    run = ledger.start_run("daily_research", "2026-09-22", trigger_source="offline")
    path = workspace / ".runs/locks/daily_research-2026-09-22.lock"
    path.parent.mkdir()
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="busy"):
            backup(workspace, tmp_path / "locked")
    result = backup(workspace, tmp_path / "snapshot")
    restore_workflow(tmp_path / "snapshot", result["manifest_hash"], tmp_path / "restored")
    restored = RunLedger(tmp_path / "restored/trading_team/.runs/ledger.db", readonly=True)
    assert restored.get_run(run)["status"] == "running"


@pytest.mark.parametrize("extra", [".env", "trading_team/.runs/ledger.db-wal", "trading_team/outputs/extra.json"])
def test_unexpected_payload_files_are_rejected(workspace, tmp_path, extra):
    from trading_team.workflow_recovery import verify_workflow_backup
    result = backup(workspace, tmp_path / "snapshot")
    path = tmp_path / "snapshot" / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("unexpected")
    with pytest.raises(ValueError):
        verify_workflow_backup(tmp_path / "snapshot", result["manifest_hash"])


def test_restore_copy_failure_has_no_success_marker(workspace, tmp_path, monkeypatch):
    from trading_team import workflow_recovery
    result = backup(workspace, tmp_path / "snapshot")
    original = workflow_recovery.write_immutable
    count = 0
    def fail(path, content):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("restore disk full")
        original(path, content)
    monkeypatch.setattr(workflow_recovery, "write_immutable", fail)
    with pytest.raises(OSError, match="disk full"):
        workflow_recovery.restore_workflow(tmp_path / "snapshot", result["manifest_hash"], tmp_path / "restored")
    assert (tmp_path / "restored").exists()
    assert not (tmp_path / "restored/restore.json").exists()
