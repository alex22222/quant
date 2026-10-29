import hashlib
import json

import pytest

from test_workflow_recovery import workspace
from trading_team.workflow_recovery import backup_workflow, restore_workflow


def prepared(workspace, tmp_path):
    root = workspace.parent
    for name in ("console/serve.py", "console/index.html", "requirements.txt", "trading_team/kimi_daily.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture runtime")
    result = backup_workflow(team=workspace, target=tmp_path / "backup")
    restored = tmp_path / "restored"
    restore_workflow(tmp_path / "backup", result["manifest_hash"], restored)
    return result["manifest_hash"], restored, root


def fingerprints(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and not p.name.endswith(("-shm", "-wal"))}


def test_restored_integrity_is_not_execution_readiness(workspace, tmp_path):
    from trading_team.recovery_readiness import review
    pin, restored, runtime = prepared(workspace, tmp_path)
    before = fingerprints(restored)
    result = review(restored=restored, manifest_hash=pin, runtime_root=runtime)
    assert result["snapshot"]["verified"] is True
    assert result["runtime"]["status"] == "matched"
    assert result["status"] == "blocked" and result["blockers"]
    assert result["execution_authorized"] is False
    assert result["external_gap_reconciled"] is False
    assert result["dependencies_verified"] is False
    assert result["manual_requirements"]
    assert fingerprints(restored) == before


@pytest.mark.parametrize("change", ["pin", "payload", "receipt", "not_restored"])
def test_bad_integrity_stops_before_business_reads(workspace, tmp_path, monkeypatch, change):
    from trading_team import recovery_readiness as module
    pin, restored, runtime = prepared(workspace, tmp_path)
    if change == "pin":
        pin = "0" * 64
    elif change == "payload":
        (restored / "trading_team/watchlist.json").write_text("changed")
    elif change == "receipt":
        receipt = json.loads((restored / "restore.json").read_text())
        receipt["execution_authorized"] = True
        (restored / "restore.json").write_text(json.dumps(receipt))
    else:
        (restored / "restore.json").unlink()
    monkeypatch.setattr(module, "operational_snapshot", lambda **_: pytest.fail("read business state before integrity"))
    result = module.review(restored=restored, manifest_hash=pin, runtime_root=runtime)
    assert result["status"] == "blocked" and result["snapshot"]["verified"] is False


def test_runtime_drift_and_missing_dependencies_stay_visible(workspace, tmp_path):
    from trading_team.recovery_readiness import review
    pin, restored, runtime = prepared(workspace, tmp_path)
    (runtime / "console/index.html").write_text("changed")
    result = review(restored=restored, manifest_hash=pin, runtime_root=runtime)
    assert result["runtime"]["status"] == "mismatch"
    assert result["runtime"]["changed"] == ["console/index.html"]
    assert result["execution_authorized"] is False


def test_prompt_drift_blocks_restore_even_when_runtime_matches(workspace, tmp_path):
    from trading_team.recovery_readiness import review
    pin, restored, runtime = prepared(workspace, tmp_path)
    (runtime / "trading_team/prompts/07_risk_manager.md").write_text("changed charter")
    result = review(restored=restored, manifest_hash=pin, runtime_root=runtime)
    assert result["runtime"]["status"] == "matched"
    assert result["prompts"]["status"] == "mismatch"
    assert "research prompt mismatch" in result["blockers"]
    assert result["execution_authorized"] is False


def test_production_runbook_drift_blocks_restore_review(workspace, tmp_path):
    from trading_team.recovery_readiness import review
    pin, restored, runtime = prepared(workspace, tmp_path)
    (runtime / "docs/MULTICA_PRODUCTION_RUNBOOK.md").write_text("revised publication contract")
    result = review(restored=restored, manifest_hash=pin, runtime_root=runtime)
    assert result["runbook"]["status"] == "mismatch"
    assert "production runbook mismatch" in result["blockers"]
    assert result["execution_authorized"] is False


def test_even_clean_local_checks_require_external_review(workspace, tmp_path, monkeypatch):
    from trading_team import recovery_readiness as module
    pin, restored, runtime = prepared(workspace, tmp_path)
    monkeypatch.setattr(module, "operational_snapshot", lambda **_: {
        "ledger_status": "available", "data": {"status": "available", "freshness": "current"},
        "research": {"status": "available", "generation": {"status": "verified"},
                     "risk_review": {"status": "validated_contract"}}})
    monkeypatch.setattr(module, "reconcile_accounts", lambda **_: {"portfolios": {
        "gate": {"status": "checked"}, "no_gate": {"status": "checked"}}})
    monkeypatch.setattr(module, "notification_summary", lambda **_: {"status": "available", "counts": {}})
    monkeypatch.setattr(module, "approval_status", lambda *_, **__: {"status": "complete"})
    result = module.review(restored=restored, manifest_hash=pin, runtime_root=runtime)
    assert result["status"] == "manual_review_required"
    assert result["blockers"] == []
    assert not result["execution_authorized"] and not result["external_gap_reconciled"]


def test_absent_dependency_manifest_is_not_falsely_called_code_drift(workspace, tmp_path):
    from trading_team.recovery_readiness import review
    for name in ("console/serve.py", "console/index.html", "trading_team/kimi_daily.py"):
        path = workspace.parent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    result = backup_workflow(team=workspace, target=tmp_path / "backup")
    restored = tmp_path / "restored"
    restore_workflow(tmp_path / "backup", result["manifest_hash"], restored)
    report = review(restored=restored, manifest_hash=result["manifest_hash"], runtime_root=workspace.parent)
    assert report["runtime"]["status"] == "matched"
    assert report["runtime"]["dependency_manifest_recorded"] is False
    assert report["dependencies_verified"] is False
