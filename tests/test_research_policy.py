import json

import pytest

from pipeline import policy


def test_checked_in_policy_freezes_all_transactional_paths():
    capabilities = json.loads(policy.POLICY_FILE.read_text())["capabilities"]
    assert capabilities and not any(capabilities.values())
    for name in capabilities:
        with pytest.raises(policy.ResearchOnlyError):
            policy.require(name)


def test_missing_or_corrupt_policy_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "missing.json"
    monkeypatch.setattr(policy, "POLICY_FILE", path)
    with pytest.raises(policy.ResearchOnlyError):
        policy.require("llm_execution")
    path.write_text("broken")
    with pytest.raises(policy.ResearchOnlyError):
        policy.require("llm_execution")


def test_llm_entrypoints_reject_before_creating_files(tmp_path):
    from trading_team.decision_pipeline import materialize_decision
    from trading_team.paper_pipeline import run_paper_day, materialize_paper_decision
    from trading_team.kimi_daily import DailyMeetingRunner
    from trading_team.multica_review_chain import run_chain
    from trading_team.daily_finalize import run_finalize_workflow
    from trading_team.execute import execute
    # Bypass constructor which would create its legacy ledger, then call public entry.
    runner = object.__new__(DailyMeetingRunner)
    operations = [lambda: materialize_decision("2026-10-22", team=tmp_path),
                  lambda: materialize_paper_decision("2026-10-22", team=tmp_path),
                  lambda: run_paper_day("2026-10-22", team=tmp_path),
                  lambda: run_finalize_workflow("2026-10-22", team=tmp_path),
                  lambda: runner.run("2026-10-22", force=True),
                  lambda: run_chain("2026-10-22", team=tmp_path, execute=True),
                  lambda: execute("2026-10-22", confirm=True)]
    for operation in operations:
        with pytest.raises(policy.ResearchOnlyError):
            operation()
    assert not list(tmp_path.rglob("*"))


def test_dispatch_approval_and_live_cannot_override():
    from trading_team.multica_adapter import MulticaCLI, dispatch_daily_issue
    from trading_team.approvals import record
    from execution.runner import main, make_broker
    from execution.broker_easytrader import EasyTraderBroker
    with pytest.raises(policy.ResearchOnlyError):
        dispatch_daily_issue("2026-10-22", None)
    with pytest.raises(policy.ResearchOnlyError):
        MulticaCLI().create_issue(title="blocked", description_file=None, assignee="nobody")
    with pytest.raises(policy.ResearchOnlyError):
        record("2026-10-22", "600000", "x", "approved", "a" * 64)
    with pytest.raises(RuntimeError, match="retired"):
        make_broker("live", {"live_enabled": True})
    with pytest.raises(RuntimeError, match="retired"):
        main(["--mode", "live", "--force"])
    with pytest.raises(RuntimeError, match="retired"):
        EasyTraderBroker().connect()


def test_console_approval_returns_403_before_reading_body():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "console/serve.py"
    spec = importlib.util.spec_from_file_location("frozen_console", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = object.__new__(module.H)
    handler.path = "/api/approve"
    sent = []
    handler._send_json = lambda status, body: sent.append((status, body))
    handler.do_POST()
    assert sent[0][0] == 403
    assert sent[0][1]["code"] == "research_only"
