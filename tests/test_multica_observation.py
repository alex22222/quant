import importlib.util
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from trading_team.run_ledger import RunLedger

IDS = {name: f"00000000-0000-4000-8000-{number:012d}" for number, name in enumerate(
    ("project_id", "autopilot_id", "leader_agent_id", "runtime_id", "daemon_id", "trigger_id"), 1)}


@pytest.fixture
def observation(tmp_path):
    team = tmp_path / "trading_team"
    state = team / ".runs/multica/bootstrap.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(IDS))
    definitions = tmp_path / "multica"
    (definitions / "runbooks").mkdir(parents=True)
    (definitions / "runbooks/daily-research.md").write_text("current runbook\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "MULTICA_PRODUCTION_RUNBOOK.md").write_text("中文生产任务说明\n")
    (definitions / "workspace.json").write_text(json.dumps({"autopilot": {
        "issue_title_template": "A-share daily research {{date}}", "cron": "43 8 * * 1-5",
        "timezone": "Asia/Shanghai"}}))
    latest = {"id": "new-run", "autopilot_id": IDS["autopilot_id"], "status": "failed",
              "triggered_at": "2026-09-22T00:43:10Z", "completed_at": "2026-09-22T00:45:09Z",
              "source": "schedule", "issue_id": "issue-new", "task_id": None,
              "failure_reason": json.dumps({"detail": "The gpt-6-astra model requires a newer Codex."})}
    payloads = {
        "autopilot get": {"autopilot": {"id": IDS["autopilot_id"], "project_id": IDS["project_id"],
            "assignee_type": "agent", "assignee_id": IDS["leader_agent_id"], "status": "active",
            "description": "中文生产任务说明\n", "issue_title_template": "A-share daily research {{date}}"},
            "triggers": [{"id": IDS["trigger_id"], "autopilot_id": IDS["autopilot_id"], "kind": "schedule",
                          "enabled": True, "cron_expression": "43 8 * * 1-5", "timezone": "Asia/Shanghai",
                          "last_fired_at": "2026-09-22T00:43:10Z", "next_run_at": "2026-09-23T00:43:00Z"}]},
        "autopilot runs": {"runs": [latest], "total": 1},
        "agent get": {"id": IDS["leader_agent_id"], "runtime_id": IDS["runtime_id"], "model": "",
                      "custom_env": {"secret": "never expose"}, "instructions": "private instructions"},
        "runtime list": [{"id": IDS["runtime_id"], "daemon_id": IDS["daemon_id"], "status": "online",
                          "metadata": {"version": "codex-cli 0.145.0"}, "provider": "codex"}],
    }
    calls = []
    class CLI:
        def _run_json(self, args, **kwargs):
            assert tuple(args[:2]) in (("autopilot", "get"), ("autopilot", "runs"), ("agent", "get"), ("runtime", "list"))
            assert kwargs["timeout"] <= 10
            calls.append(args)
            value = payloads[" ".join(args[:2])]
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)
    return team, CLI(), payloads, calls


def observe(fixture):
    from trading_team.multica_observation import observe_multica
    team, cli, _, _ = fixture
    return observe_multica(team=team, cli=cli)


def test_remote_failure_is_not_hidden_by_online_runtime(observation):
    team, _, _, calls = observation
    before = {str(p): p.read_bytes() for p in team.parent.rglob("*") if p.is_file()}
    result = observe(observation)
    assert result["status"] == "attention"
    assert result["history"]["latest"]["status"] == "failed"
    assert "newer Codex" in result["history"]["latest"]["failure_reason"]
    assert result["runtime"]["remote_status"] == "online"
    assert result["runtime"]["version"] == "codex-cli 0.145.0"
    assert result["local_attempt"]["status"] == "missing"
    assert result["completion_verified"] is False
    assert len(calls) == 4
    assert {str(p): p.read_bytes() for p in team.parent.rglob("*") if p.is_file()} == before
    assert "never expose" not in json.dumps(result) and "private instructions" not in json.dumps(result)


def test_read_only_observation_works_while_project_task_context_is_active(observation, monkeypatch):
    from trading_team import multica_observation
    from trading_team.multica_adapter import MulticaCLI, ROOT

    team, _, payloads, _ = observation
    workdirs = []

    def runner(args, **kwargs):
        workdir = Path(kwargs["cwd"]).resolve()
        workdirs.append(workdir)
        if workdir.is_relative_to(ROOT):
            return subprocess.CompletedProcess(args, 1, "", "agent execution context requires MULTICA_TOKEN")
        return subprocess.CompletedProcess(args, 0, json.dumps(payloads[" ".join(args[1:3])]), "")

    monkeypatch.setattr(multica_observation, "MulticaCLI",
                        lambda **kwargs: MulticaCLI(binary="multica", runner=runner, **kwargs))
    result = multica_observation.observe_multica(team=team)
    assert result["configuration"]["status"] == "matched"
    assert result["history"]["status"] == "available"
    assert result["runtime"]["status"] == "matched"
    assert len(workdirs) == 4 and all(not path.is_relative_to(ROOT) for path in workdirs)


@pytest.mark.parametrize("change", ["project", "assignee", "identity"])
def test_wrong_remote_binding_stops_project_queries(observation, change):
    _, _, payloads, calls = observation
    auto = payloads["autopilot get"]["autopilot"]
    auto[{"project": "project_id", "assignee": "assignee_id", "identity": "id"}[change]] = "other-project"
    result = observe(observation)
    assert result["status"] == "mismatch"
    assert result["history"]["status"] == "unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["runbook", "title", "cron", "disabled", "duplicate"])
def test_remote_configuration_drift_is_visible_without_repair(observation, change):
    _, _, payloads, calls = observation
    auto = payloads["autopilot get"]
    if change == "runbook":
        auto["autopilot"]["description"] = "old runbook"
    elif change == "title":
        auto["autopilot"]["issue_title_template"] = "undated"
    elif change == "cron":
        auto["triggers"][0]["cron_expression"] = "0 9 * * *"
    elif change == "disabled":
        auto["triggers"][0]["enabled"] = False
    else:
        auto["triggers"].append(dict(auto["triggers"][0], id="duplicate"))
    result = observe(observation)
    assert result["configuration"]["status"] == "drift"
    assert result["configuration"]["differences"]
    assert result["history"]["latest"]["status"] == "failed"
    assert all("update" not in args and "trigger-add" not in args for args in calls)


def test_latest_remote_failure_wins_over_older_success_even_if_unsorted(observation):
    _, _, payloads, _ = observation
    old = dict(payloads["autopilot runs"]["runs"][0], id="old", status="succeeded", triggered_at="2026-09-21T00:43:10Z")
    payloads["autopilot runs"]["runs"].insert(0, old)
    payloads["autopilot runs"]["total"] = 2
    assert observe(observation)["history"]["latest"]["id"] == "new-run"


@pytest.mark.parametrize("history", [{}, {"runs": [{}], "total": 1},
                                    {"runs": "invalid", "total": 0},
                                    RuntimeError("network failed: Bearer PRIVATE")])
def test_unreadable_history_does_not_become_empty_or_success(observation, history):
    _, _, payloads, _ = observation
    payloads["autopilot runs"] = history
    result = observe(observation)
    assert result["history"]["status"] == "unavailable"
    assert result["status"] == "attention"
    assert "PRIVATE" not in json.dumps(result)


def test_empty_remote_history_is_explicit_not_completed(observation):
    observation[2]["autopilot runs"] = {"runs": [], "total": 0}
    result = observe(observation)
    assert result["history"]["status"] == "empty"
    assert result["completion_verified"] is False


@pytest.mark.parametrize("change", ["missing", "binding", "daemon"])
def test_runtime_drift_does_not_select_another_online_runtime(observation, change):
    payloads = observation[2]
    if change == "missing":
        payloads["runtime list"][0]["id"] = "another-online-runtime"
    elif change == "binding":
        payloads["agent get"]["runtime_id"] = "another-online-runtime"
    else:
        payloads["runtime list"][0]["daemon_id"] = "other-daemon"
    result = observe(observation)
    assert result["runtime"]["status"] in ("missing", "mismatch")
    assert result["completion_verified"] is False


def test_successful_remote_run_without_verified_local_attempt_stays_unverified(observation):
    team, _, payloads, _ = observation
    payloads["autopilot runs"]["runs"][0].update(status="succeeded", task_id="task-new", failure_reason=None)
    ledger = RunLedger(team / ".runs/ledger.db")
    run_id = ledger.start_run("daily_research", "2026-09-22", trigger_source="fixture", external_context={
        "provider": "multica", "verification": "unverified", "issue_id": "issue-new", "task_id": "task-new"})
    ledger.finish_run(run_id, status="succeeded")
    assert observe(observation)["local_attempt"]["status"] == "missing"
    with ledger._connect() as conn:
        conn.execute("UPDATE workflow_attempts SET external_context_json=? WHERE run_id=?", (json.dumps({
            "provider": "multica", "verification": "remote_verified", "issue_id": "issue-new", "task_id": "task-new",
            "project_id": IDS["project_id"]}), run_id))
    result = observe(observation)
    assert result["local_attempt"]["run_id"] == run_id
    assert result["local_attempt"]["run_status"] == "succeeded"
    assert result["completion_verified"] is False


def test_api_reads_only_current_workspace_observation(tmp_path, monkeypatch):
    from trading_team import multica_observation
    spec = importlib.util.spec_from_file_location("multica_console_test", Path(__file__).resolve().parents[1] / "console/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(multica_observation, "observe_multica", lambda **kwargs: calls.append(kwargs) or {"status": "attention"})
    handler = object.__new__(module.H)
    handler.path = "/api/multica-observation"
    sent = []
    handler._send_json = lambda status, payload: sent.append((status, payload))
    handler.do_GET()
    assert calls == [{"team": tmp_path / "trading_team"}]
    assert sent == [(200, {"ok": True, "status": "attention"})]
    assert not list(tmp_path.iterdir())
