import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from trading_team import multica_adapter as adapter
from trading_team.run_ledger import RunLedger

DAY = "2026-09-22"
IDS = {key: f"00000000-0000-4000-8000-{number:012d}"
       for number, key in enumerate(("issue", "task", "agent", "workspace", "project", "squad"), 1)}


@pytest.fixture
def execution(tmp_path, monkeypatch):
    state_path = tmp_path / "bootstrap.json"
    state_path.write_text(json.dumps({
        "project_id": IDS["project"], "squad_id": IDS["squad"],
        "agent_ids": {"Research Lead": IDS["agent"]},
    }))
    env = {f"MULTICA_{key.upper()}_ID": IDS[key] for key in ("task", "agent", "workspace")}
    env["MULTICA_TOKEN"] = "secret-must-never-be-persisted"
    issue = {"id": IDS["issue"], "identifier": "ASH-1", "project_id": IDS["project"],
             "workspace_id": IDS["workspace"], "title": f"A-share daily research {DAY}"}
    remote_run = {"id": IDS["task"], "agent_id": IDS["agent"], "status": "running"}
    cli = adapter.MulticaCLI(binary="multica")

    def call(args):
        if args[:2] == ["issue", "get"]:
            return deepcopy(issue)
        assert args == ["issue", "runs", IDS["issue"], "--output", "json"]
        return [deepcopy(remote_run)]

    monkeypatch.setattr(cli, "_run_json", call)
    return cli, state_path, env, issue, remote_run


def verify(execution):
    cli, state_path, env, _, _ = execution
    return adapter.verify_execution_context(DAY, IDS["issue"], cli=cli,
                                            environ=env, state_path=state_path)


def test_execution_context_is_remote_verified_and_secret_free(execution):
    result = verify(execution)
    assert result["task_id"] == IDS["task"]
    assert result["issue_id"] == IDS["issue"]
    assert result["role"] == "Research Lead"
    assert result["verification"] == "remote_verified"
    assert "secret" not in json.dumps(result)


def test_new_daily_issue_accepts_chinese_title_without_loosening_date(execution):
    cli, state_path, env, issue, _ = execution
    issue["title"] = "A股每日研究 2026-09-24"
    result = adapter.verify_execution_context("2026-09-24", IDS["issue"], cli=cli,
                                              environ=env, state_path=state_path)
    assert result["issue_title"] == issue["title"]
    issue["title"] = "A股每日研究 2026-09-23"
    with pytest.raises(ValueError, match="title"):
        adapter.verify_execution_context("2026-09-24", IDS["issue"], cli=cli,
                                         environ=env, state_path=state_path)


@pytest.mark.parametrize("kind", ["missing_identity", "invalid_identity", "wrong_project",
                                  "wrong_workspace", "wrong_date", "unknown_agent", "wrong_run",
                                  "wrong_agent", "terminal_run", "network_failure"])
def test_execution_context_fails_closed(execution, kind, monkeypatch):
    cli, _, env, issue, remote_run = execution
    if kind == "missing_identity":
        env.pop("MULTICA_TASK_ID")
    elif kind == "invalid_identity":
        env["MULTICA_TASK_ID"] = "not-a-task"
    elif kind == "wrong_project":
        issue["project_id"] = IDS["squad"]
    elif kind == "wrong_workspace":
        issue["workspace_id"] = IDS["squad"]
    elif kind == "wrong_date":
        issue["title"] = "A-share daily research 2026-09-21"
    elif kind == "unknown_agent":
        env["MULTICA_AGENT_ID"] = IDS["squad"]
    elif kind == "wrong_run":
        remote_run["id"] = IDS["squad"]
    elif kind == "wrong_agent":
        remote_run["agent_id"] = IDS["squad"]
    elif kind == "terminal_run":
        remote_run["status"] = "completed"
    else:
        def fail(_args):
            raise RuntimeError("network failure")
        monkeypatch.setattr(cli, "_run_json", fail)
    with pytest.raises((ValueError, RuntimeError)):
        verify(execution)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_integration_audit_records_exact_remote_identity_and_test_outcome(execution, tmp_path, exit_code):
    cli, state_path, env, issue, _ = execution
    state = json.loads(state_path.read_text())
    state["agent_ids"] = {"Audit Verifier": IDS["agent"]}
    state_path.write_text(json.dumps(state))
    issue["title"] = f"A-share integration audit {DAY}"
    ledger = RunLedger(tmp_path / "runs.db")
    called = []

    def run(command, **kwargs):
        called.append(command)
        return SimpleNamespace(returncode=exit_code, stdout="offline test result", stderr="")

    result = adapter.run_integration_audit(
        DAY, IDS["issue"], cli=cli, environ=env, state_path=state_path,
        ledger=ledger, output_dir=tmp_path / "audit", runner=run,
    )
    assert result["status"] == ("succeeded" if exit_code == 0 else "failed")
    assert called[0][1:] == ["-m", "pytest", "tests", "execution/tests", "-q"]
    recorded = ledger.get_run(result["run_id"])
    assert recorded["attempts"][0]["external_context"]["task_id"] == IDS["task"]
    assert recorded["steps"][0]["metadata"]["returncode"] == exit_code
    assert "secret" not in json.dumps(recorded)


def test_integration_audit_rejects_non_verifier_before_tests(execution, tmp_path):
    cli, state_path, env, issue, _ = execution
    issue["title"] = f"A-share integration audit {DAY}"
    ledger = RunLedger(tmp_path / "runs.db")
    with pytest.raises(ValueError, match="Audit Verifier"):
        adapter.run_integration_audit(DAY, IDS["issue"], cli=cli, environ=env,
                                      state_path=state_path, ledger=ledger)
    assert ledger.list_runs() == []


def test_integration_audit_rejects_code_drift_despite_green_tests(execution, tmp_path, monkeypatch):
    cli, state_path, env, issue, _ = execution
    state = json.loads(state_path.read_text())
    state["agent_ids"] = {"Audit Verifier": IDS["agent"]}
    state_path.write_text(json.dumps(state))
    issue["title"] = f"A-share integration audit {DAY}"
    versions = iter([{"module.py": "before"}, {"module.py": "after"}])
    monkeypatch.setattr(adapter, "_audit_code_hashes", lambda: next(versions))
    ledger = RunLedger(tmp_path / "runs.db")
    result = adapter.run_integration_audit(
        DAY, IDS["issue"], cli=cli, environ=env, state_path=state_path,
        ledger=ledger, output_dir=tmp_path / "audit",
        runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="passed", stderr=""),
    )
    assert result["status"] == "failed"
    assert ledger.get_run(result["run_id"])["steps"][0]["metadata"]["code_unchanged"] is False
