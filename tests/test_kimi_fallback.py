# -*- coding: utf-8 -*-
import json
import sys

import pytest

from trading_team.kimi_fallback import latest_scheduled_run, quota_exhausted
from trading_team import kimi_fallback

DAY = "2026-09-22"


@pytest.fixture
def isolated_fallback(tmp_path, monkeypatch):
    team = tmp_path / "trading_team"
    monkeypatch.setattr(kimi_fallback, "TEAM", team)
    monkeypatch.setattr(kimi_fallback, "latest_scheduled_run", lambda day: {
        "runId": "primary-1", "status": "succeeded",
    })
    def forbidden(*args, **kwargs):
        pytest.fail("fallback must not read credentials or call a model")
    monkeypatch.setattr(kimi_fallback, "load_local_env", forbidden)
    monkeypatch.setattr(kimi_fallback, "KimiClient", forbidden)
    return team


def _write_run(root, run_id, started, status="failed", scheduled=True):
    path = root / "runs" / run_id / "run.json"
    path.parent.mkdir(parents=True)
    trigger = "schedule" if scheduled else "manual"
    path.write_text(json.dumps({"run": {
        "runId": run_id,
        "startedAt": started,
        "status": status,
        "logs": [f"Automation.run triggered by {trigger}"],
    }}), encoding="utf-8")


def test_latest_scheduled_run_uses_shanghai_date(tmp_path):
    _write_run(tmp_path, "old", "2026-08-29T00:43:00Z")
    _write_run(tmp_path, "today", "2026-08-30T00:43:00Z")
    _write_run(tmp_path, "manual", "2026-08-30T01:00:00Z", scheduled=False)
    assert latest_scheduled_run("2026-08-30", tmp_path)["runId"] == "today"


def test_quota_detection_requires_run_id_and_marker(tmp_path):
    (tmp_path / "main.log").write_text(
        "run_other resource_exhausted\n"
        "run_target WORKER_FAILED: insufficient balance\n",
        encoding="utf-8",
    )
    assert quota_exhausted("run_target", tmp_path) is True
    assert quota_exhausted("run_missing", tmp_path) is False


@pytest.mark.parametrize("name", ["daily_conclusion.md", "plan.json", "01_fundamental.md"])
def test_legacy_or_partial_output_blocks_fallback_without_overwriting(isolated_fallback, name):
    output = isolated_fallback / "outputs" / DAY
    output.mkdir(parents=True)
    path = output / name
    path.write_text("legacy report")
    result = kimi_fallback.fallback(DAY)
    assert result["status"] == "blocked"
    assert result["code"] == "existing_generation_unverified"
    assert path.read_text() == "legacy report"
    assert not (isolated_fallback / ".runs").exists()


def test_primary_success_without_generation_is_not_success(isolated_fallback):
    result = kimi_fallback.fallback(DAY)
    assert result["status"] == "blocked"
    assert result["code"] == "primary_completed_without_generation"
    assert not isolated_fallback.exists()


def test_running_primary_is_not_replaced_even_with_partial_output(isolated_fallback, monkeypatch):
    monkeypatch.setattr(kimi_fallback, "latest_scheduled_run", lambda day: {"runId": "primary-1", "status": "running"})
    output = isolated_fallback / "outputs" / DAY
    output.mkdir(parents=True)
    (output / "daily_conclusion.md").write_text("partial")
    result = kimi_fallback.fallback(DAY)
    assert result["status"] == "skipped"
    assert "仍在运行" in result["reason"]


def test_nonquota_failure_cannot_use_api(isolated_fallback, monkeypatch):
    monkeypatch.setattr(kimi_fallback, "latest_scheduled_run", lambda day: {"runId": "primary-1", "status": "failed"})
    monkeypatch.setattr(kimi_fallback, "quota_exhausted", lambda run_id: False)
    assert kimi_fallback.fallback(DAY)["status"] == "blocked"


def test_quota_check_is_readonly_and_real_fallback_records_primary_id(isolated_fallback, monkeypatch):
    monkeypatch.setattr(kimi_fallback, "latest_scheduled_run", lambda day: {"runId": "primary-1", "status": "failed"})
    monkeypatch.setattr(kimi_fallback, "quota_exhausted", lambda run_id: run_id == "primary-1")
    assert kimi_fallback.fallback(DAY, check_only=True)["status"] == "fallback_required"
    calls = []
    class Runner:
        def __init__(self, client, team):
            assert client == "fixture-client" and team == isolated_fallback
        def run(self, day, **kwargs):
            calls.append((day, kwargs))
            return {"status": "succeeded"}
    monkeypatch.setattr(kimi_fallback, "load_local_env", lambda: None)
    monkeypatch.setattr(kimi_fallback, "KimiClient", lambda *args: "fixture-client")
    monkeypatch.setattr(kimi_fallback, "DailyMeetingRunner", Runner)
    assert kimi_fallback.fallback(DAY)["status"] == "succeeded"
    assert calls == [(DAY, {"trigger_source": "kimi_quota_fallback:primary-1"})]


@pytest.mark.parametrize("status,code", [("blocked", 1), ("fallback_required", 2), ("skipped", 0)])
def test_cli_exposes_non_success_exit_status(monkeypatch, capsys, status, code):
    monkeypatch.setattr(sys, "argv", ["kimi_fallback", "--date", DAY, "--check"])
    monkeypatch.setattr(kimi_fallback, "fallback", lambda day, check_only=False: {"date": day, "status": status})
    with pytest.raises(SystemExit) as stopped:
        kimi_fallback.main()
    assert stopped.value.code == code
    assert json.loads(capsys.readouterr().out)["status"] == status
