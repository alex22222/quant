import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from test_workflow_recovery import workspace

NOW = datetime(2026, 9, 22, 8, tzinfo=ZoneInfo("Asia/Shanghai"))


def service():
    from trading_team import backup_service
    return backup_service


def configured(workspace, tmp_path):
    module = service()
    store = tmp_path / "persistent-backups"
    module.initialize(team=workspace, store=store, now=NOW)
    return module, store


def test_status_is_readonly_and_does_not_initialize(tmp_path):
    team = tmp_path / "trading_team"
    assert service().service_status(team=team, now=NOW)["status"] == "uninitialized"
    assert not team.exists()


def test_initialization_binds_private_external_store_and_is_idempotent(workspace, tmp_path):
    module, store = configured(workspace, tmp_path)
    config = workspace / ".runs/backup-service.json"
    before = config.read_bytes()
    assert module.initialize(team=workspace, store=store, now=NOW + timedelta(hours=1))["status"] == "configured"
    assert config.read_bytes() == before
    assert store.stat().st_mode & 0o777 == 0o700
    assert (store / "catalog.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        module.initialize(team=workspace, store=tmp_path / "different", now=NOW)
    assert not (tmp_path / "different").exists()


@pytest.mark.parametrize("kind", ["inside", "ancestor", "symlink", "unowned"])
def test_initialization_refuses_unsafe_store(workspace, tmp_path, kind):
    store = tmp_path / "store"
    if kind == "inside":
        store = workspace / "backups"
    elif kind == "ancestor":
        store = workspace.parent.parent
    elif kind == "symlink":
        store.symlink_to(tmp_path / "outside")
    else:
        store.mkdir()
        (store / "unrelated.txt").write_text("preserve")
    with pytest.raises(ValueError):
        service().initialize(team=workspace, store=store, now=NOW)
    assert not (workspace / ".runs/backup-service.json").exists()


def test_tick_backups_once_per_interval_and_pins_exact_snapshot(workspace, tmp_path):
    module, store = configured(workspace, tmp_path)
    first = module.tick(team=workspace, now=NOW)
    assert first["status"] == "healthy" and first["created"] is True
    latest = first["latest"]
    assert Path(latest["path"]).is_relative_to(store)
    from trading_team.workflow_recovery import verify_workflow_backup
    manifest = verify_workflow_backup(Path(latest["path"]), latest["manifest_hash"])
    assert manifest["source_root"] == str(workspace.parent)
    assert first["offsite_verified"] is False
    assert first["execution_authorized"] is False
    assert module.tick(team=workspace, now=NOW + timedelta(minutes=15))["created"] is False
    second = module.tick(team=workspace, now=NOW + timedelta(hours=6))
    assert second["created"] and second["snapshot_count"] == 2
    assert second["latest"]["path"] != latest["path"]


def test_new_paper_cohort_requires_fresh_registered_snapshot(workspace, tmp_path):
    from trading_team.paper_pipeline import _db, paper_db_paths
    from trading_team.workflow_recovery import verify_workflow_backup

    module, store = configured(workspace, tmp_path)
    first = module.tick(team=workspace, now=NOW)
    config = workspace.parent / "paper/active_cohort.json"
    config.write_text(json.dumps({
        "schema": "paper_cohort.v1", "cohort": "2026-09-28-forward",
        "start_date": "2026-09-28", "initial_cash": 100000,
        "accounts": {name: f"paper/cohort_2026-09-28/llm_{name}.db"
                     for name in ("gate", "no_gate")},
    }))
    for path in paper_db_paths(workspace).values():
        _db(path).close()

    report = module.service_status(team=workspace, now=NOW + timedelta(minutes=15))
    assert report["status"] == "degraded"
    assert "active Paper cohort not in latest snapshot" in report["issues"]
    result = module.tick(team=workspace, now=NOW + timedelta(minutes=15))
    assert result["created"] and result["status"] == "healthy"
    assert result["snapshot_count"] == 2
    assert result["latest"]["path"] != first["latest"]["path"]
    manifest = verify_workflow_backup(Path(result["latest"]["path"]), result["latest"]["manifest_hash"])
    assert "paper/active_cohort.json" in manifest["files"]
    assert {path.relative_to(workspace.parent).as_posix() for path in paper_db_paths(workspace).values()} <= set(manifest["databases"])


def test_current_runtime_and_prompts_are_not_hidden_by_valid_snapshot(workspace, tmp_path):
    module, _ = configured(workspace, tmp_path)
    runner = workspace / "kimi_daily.py"
    runner.write_text("original runner")
    first = module.tick(team=workspace, now=NOW)
    assert first["status"] == "healthy"

    runner.write_text("changed runner")
    prompt = workspace / "prompts/07_risk_manager.md"
    prompt.write_text("changed charter")
    report = module.service_status(team=workspace, now=NOW + timedelta(minutes=15))
    assert report["latest"]["verified"] is True
    assert report["status"] == "degraded"
    assert report["runtime_snapshot_current"] is False
    assert report["prompts_snapshot_current"] is False
    assert "runtime differs from latest snapshot" in report["issues"]
    assert "prompts differ from latest snapshot" in report["issues"]
    assert module.tick(team=workspace, now=NOW + timedelta(minutes=15))["created"] is False

    renewed = module.tick(team=workspace, now=NOW + timedelta(hours=6))
    assert renewed["created"] is True
    assert renewed["status"] == "healthy"
    assert renewed["runtime_snapshot_current"] is True
    assert renewed["prompts_snapshot_current"] is True


def test_production_runbook_drift_is_not_hidden_by_valid_snapshot(workspace, tmp_path):
    module, _ = configured(workspace, tmp_path)
    first = module.tick(team=workspace, now=NOW)
    assert first["status"] == "healthy"

    runbook = workspace.parent / "docs/MULTICA_PRODUCTION_RUNBOOK.md"
    runbook.write_text("revised publication contract")
    report = module.service_status(team=workspace, now=NOW + timedelta(minutes=15))
    assert report["latest"]["verified"] is True
    assert report["status"] == "degraded"
    assert report["runbook_snapshot_current"] is False
    assert "production runbook differs from latest snapshot" in report["issues"]

    renewed = module.tick(team=workspace, now=NOW + timedelta(minutes=15))
    assert renewed["created"] is True
    assert renewed["runbook_snapshot_current"] is True
    assert module.tick(team=workspace, now=NOW + timedelta(minutes=30))["created"] is False


def test_missing_production_runbook_does_not_create_repeated_snapshots(workspace, tmp_path):
    module, _ = configured(workspace, tmp_path)
    first = module.tick(team=workspace, now=NOW)
    (workspace.parent / "docs/MULTICA_PRODUCTION_RUNBOOK.md").unlink()

    result = module.tick(team=workspace, now=NOW + timedelta(minutes=15))
    assert result["status"] == "degraded"
    assert result["created"] is False
    assert result["snapshot_count"] == first["snapshot_count"]


def test_missing_or_corrupt_catalog_is_not_reinitialized(workspace, tmp_path):
    module, store = configured(workspace, tmp_path)
    module.tick(team=workspace, now=NOW)
    (store / "catalog.json").write_text("{broken")
    assert module.service_status(team=workspace, now=NOW)["status"] == "invalid"
    with pytest.raises(ValueError):
        module.tick(team=workspace, now=NOW)
    assert (store / "catalog.json").read_text() == "{broken"
    (store / "catalog.json").unlink()
    with pytest.raises(ValueError):
        module.tick(team=workspace, now=NOW)
    assert not (store / "catalog.json").exists()


def test_payload_corruption_is_detected_readonly_and_retained(workspace, tmp_path):
    module, store = configured(workspace, tmp_path)
    first = module.tick(team=workspace, now=NOW)
    path = Path(first["latest"]["path"]) / "trading_team/watchlist.json"
    path.write_text("corrupted evidence")
    before = (store / "catalog.json").read_bytes()
    report = module.service_status(team=workspace, now=NOW)
    assert report["status"] == "degraded" and report["issues"]
    assert (store / "catalog.json").read_bytes() == before
    result = module.tick(team=workspace, now=NOW + timedelta(minutes=15))
    assert result["created"] and result["status"] == "degraded"
    assert path.read_text() == "corrupted evidence"
    assert result["snapshot_count"] == 2


def test_failed_backup_preserves_partial_directory_then_uses_new_attempt(workspace, tmp_path, monkeypatch):
    module, store = configured(workspace, tmp_path)
    original = module.backup_workflow
    def fail(*, team, target):
        target.mkdir(parents=True)
        (target / "partial.json").write_text("partial")
        raise OSError("disk interruption")
    monkeypatch.setattr(module, "backup_workflow", fail)
    first = module.tick(team=workspace, now=NOW)
    assert first["status"] == "degraded" and first["snapshot_count"] == 0
    partial = next((store / "snapshots").glob("*/partial.json"))
    monkeypatch.setattr(module, "backup_workflow", original)
    assert not module.tick(team=workspace, now=NOW + timedelta(seconds=30))["created"]
    later = module.tick(team=workspace, now=NOW + timedelta(minutes=15))
    assert later["snapshot_count"] == 1 and later["created"]
    assert partial.read_text() == "partial"
    assert later["attempt_count"] == 2


def test_verified_payload_without_catalog_ack_is_not_silently_adopted(workspace, tmp_path, monkeypatch):
    module, store = configured(workspace, tmp_path)
    original = module._save_catalog
    def crash(store, catalog):
        if any(a["status"] == "succeeded" for a in catalog["attempts"]):
            raise SystemExit("crash before catalog ack")
        original(store, catalog)
    monkeypatch.setattr(module, "_save_catalog", crash)
    with pytest.raises(SystemExit):
        module.tick(team=workspace, now=NOW)
    assert module.service_status(team=workspace, now=NOW)["snapshot_count"] == 0
    orphan = next((store / "snapshots").glob("*/manifest.json"))
    before = orphan.read_bytes()
    monkeypatch.setattr(module, "_save_catalog", original)
    result = module.tick(team=workspace, now=NOW + timedelta(minutes=15))
    assert result["snapshot_count"] == 1 and result["attempt_count"] == 2
    assert orphan.read_bytes() == before
    assert result["latest"]["path"] != str(orphan.parent)
    assert result["unregistered_count"] == 1


def test_busy_shared_writer_does_not_start_backup_attempt(workspace, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from trading_team.state_lock import state_lock, StateBusy
    module, store = configured(workspace, tmp_path)
    before = (store / "catalog.json").read_bytes()
    with state_lock(workspace), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(module.tick, team=workspace, now=NOW)
        with pytest.raises(StateBusy):
            future.result()
    assert (store / "catalog.json").read_bytes() == before


def test_low_space_or_capacity_does_not_delete_existing_backups(workspace, tmp_path, monkeypatch):
    module, store = configured(workspace, tmp_path)
    first = module.tick(team=workspace, now=NOW)
    manifest = Path(first["latest"]["path"]) / "manifest.json"
    before = manifest.read_bytes()
    monkeypatch.setattr(module, "MAX_STORE_BYTES", 1)
    result = module.tick(team=workspace, now=NOW + timedelta(hours=6))
    assert result["status"] == "degraded" and result["created"] is False
    assert manifest.read_bytes() == before
    assert result["snapshot_count"] == 1


def test_copied_host_config_cannot_write_original_store(tmp_path, workspace):
    module, store = configured(workspace, tmp_path)
    copied = tmp_path / "restored/trading_team"
    (copied / ".runs").mkdir(parents=True)
    (copied / ".runs/backup-service.json").write_bytes((workspace / ".runs/backup-service.json").read_bytes())
    before = (store / "catalog.json").read_bytes()
    with pytest.raises(ValueError, match="source"):
        module.tick(team=copied, now=NOW)
    assert (store / "catalog.json").read_bytes() == before


def test_stale_heartbeat_is_not_hidden_by_recent_valid_backup(workspace, tmp_path):
    module, _ = configured(workspace, tmp_path)
    module.tick(team=workspace, now=NOW)
    result = module.service_status(team=workspace, now=NOW + timedelta(hours=1))
    assert result["status"] == "degraded"
    assert result["heartbeat"] == "stale" and result["latest"]["verified"] is True


def test_initialize_does_not_follow_source_state_symlink(tmp_path):
    team = tmp_path / "source/trading_team"
    team.mkdir(parents=True)
    external = tmp_path / "unrelated"
    external.mkdir()
    (team / ".runs").symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        service().initialize(team=team, store=tmp_path / "backups", now=NOW)
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("key,value", [("integrity_failures", ["invalid"]),
    ("last_check_at", "invalid"), ("attempts", [{"id": None}])])
def test_malformed_catalog_is_fail_closed_without_rewrite(workspace, tmp_path, key, value):
    module, store = configured(workspace, tmp_path)
    path = store / "catalog.json"
    data = json.loads(path.read_text())
    data[key] = value
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    assert module.service_status(team=workspace, now=NOW)["status"] == "invalid"
    with pytest.raises((ValueError, TypeError)):
        module.tick(team=workspace, now=NOW)
    assert path.read_bytes() == before


def test_future_clock_and_catalog_path_drift_are_rejected(workspace, tmp_path):
    module, store = configured(workspace, tmp_path)
    module.tick(team=workspace, now=NOW)
    path = store / "catalog.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="clock"):
        module.tick(team=workspace, now=NOW - timedelta(minutes=1))
    assert path.read_bytes() == before
    data = json.loads(before)
    data["attempts"][0]["path"] = str(tmp_path / "outside")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="path"):
        module.tick(team=workspace, now=NOW)


def test_latest_only_verification_is_explicit(workspace, tmp_path):
    module, _ = configured(workspace, tmp_path)
    first = module.tick(team=workspace, now=NOW)
    module.tick(team=workspace, now=NOW + timedelta(hours=6))
    (Path(first["latest"]["path"]) / "trading_team/watchlist.json").write_text("older corruption")
    result = module.service_status(team=workspace, now=NOW + timedelta(hours=6))
    assert result["status"] == "healthy" and result["latest"]["verified"]
    assert result["verification_scope"] == "latest_snapshot_only"


def test_private_host_configuration_is_not_restored(workspace, tmp_path):
    module, _ = configured(workspace, tmp_path)
    result = module.tick(team=workspace, now=NOW)
    from trading_team.workflow_recovery import restore_workflow
    restore_workflow(Path(result["latest"]["path"]), result["latest"]["manifest_hash"], tmp_path / "restore")
    assert not (tmp_path / "restore/trading_team/.runs/backup-service.json").exists()
