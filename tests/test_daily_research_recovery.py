import json
import fcntl
from collections import Counter
from datetime import date, datetime

import pytest
import numpy as np

from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.daily_finalize import materialize_day
from trading_team.kimi_daily import DailyMeetingRunner, ROLE_NAMES
from trading_team.run_ledger import RunLedger

pytestmark = pytest.mark.usefixtures("finalize_calendar")


DAY = "2026-09-21"


class ScriptedClient:
    model = "offline-test"
    last_meta = {}
    base_url = "https://fixture.invalid/v1"
    temperature = 0.6

    @property
    def transport_identity(self):
        return {"schema": "model_transport.v1", "adapter": "offline_fixture.v1",
                "endpoint": self.base_url, "model": self.model,
                "parameters": {"temperature": self.temperature, "max_tokens": 8192,
                               "thinking": "disabled", "response_format": "json_object"}}

    def __init__(self):
        self.calls = Counter()
        self.fail_role = None

    def complete(self, system, user):
        self.last_meta = {"transport": self.transport_identity, "reported_model": "offline-fixture-revision"}
        role = system.splitlines()[0]
        self.calls[role] += 1
        if role == self.fail_role:
            self.fail_role = None
            raise RuntimeError("injected model interruption")
        block = {"role": role, "date": DAY}
        result = {}
        if role == "trader":
            result.update(market_ok=True, proposals=[{
                "code": "601318", "name": "China Ping An", "direction": "条件买入",
                "position_pct": 0.08, "entry": [53.5], "entry_type": "limit",
                "stop": 50.6, "condition": None, "horizon": "2 weeks",
                "confidence": 3,
            }])
        elif role == "risk_manager":
            block["reviews"] = [{"code": "601318", "decision": "通过", "reason_codes": []}]
            result["daily_conclusion_markdown"] = "## 一句话总览\nOffline fixture."
        else:
            block["ratings"] = [{"code": "601318", "rating": "看多", "confidence": 3}]
            if role == "news":
                block.update(items=[], temperature=0)
        result["report_markdown"] = "Report\n```json\n" + json.dumps(block) + "\n```"
        return result


@pytest.fixture
def meeting(tmp_path, monkeypatch, execution_cache, finalize_calendar):
    team = tmp_path / "trading_team"
    context = team / "context" / DAY
    context.mkdir(parents=True)
    (context / "technical_000300.json").write_text('{"close":101,"ma60":100}')
    (context / "technical_601318.json").write_text('{"close":54}')
    (context / "fundamental_601318.json").write_text('{"pe":10}')
    for name in ("news", "sentiment", "global_macro"):
        (context / f"{name}.json").write_text("{}")
    (team / "watchlist.json").write_text('{"stocks":[{"code":"601318"}]}')
    from trading_team.market_archive import capture_closing
    bar = {"open": 54, "high": 55, "low": 53, "close": 54, "prev_close": 54}
    execution_cache(team, "2026-09-18", {code: bar for code in ("601318", "000300")})
    receipt = capture_closing(DAY, team=team, calendar_path=finalize_calendar,
                              now=datetime(2026, 9, 21, 8))
    (context / "execution_archive.json").write_text(json.dumps(receipt))
    (context / "manifest.json").write_text(json.dumps({
        "date": DAY, "ok": True, "latest_trade_date": "2026-09-18",
        "sources": {"technical": {"ok": True}, "execution_archive": {"ok": True}},
        "execution_archive": receipt,
    }))
    (team / "prompts").mkdir()
    for number, role in ROLE_NAMES.items():
        (team / "prompts" / f"{number}_{role}.md").write_text(role)
    client = ScriptedClient()
    runner = DailyMeetingRunner(client, team=team, ledger=RunLedger(team / ".runs/ledger.db"))
    postprocess_calls = []

    def postprocess(run_id, day, push):
        postprocess_calls.append(run_id)
        materialize_day(day, team)
        return {"notification": {"status": "disabled"}}

    monkeypatch.setattr(runner, "_postprocess", postprocess)
    return runner, client, postprocess_calls


@pytest.mark.parametrize("broken", ["missing", "object", "date", "coverage"])
def test_skip_collect_cannot_bypass_raw_archive_gate(meeting, broken):
    runner, client, _ = meeting
    path = runner.team / "context" / DAY / "manifest.json"
    manifest = json.loads(path.read_text())
    if broken == "missing":
        manifest.pop("execution_archive")
    elif broken == "object":
        receipt = manifest["execution_archive"]
        (runner.team / "market_archive" / receipt["date"] / f"{receipt['payload_hash']}.json").write_text("bad")
    elif broken == "date":
        manifest["latest_trade_date"] = DAY
    else:
        (runner.team / "watchlist.json").write_text('{"stocks":[{"code":"600036"}]}')
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="archive|行情|coverage"):
        runner.run(DAY, collect=False, push=False)
    assert not client.calls


def test_full_generation_and_unchanged_reuse(meeting):
    runner, client, postprocess_calls = meeting
    first = runner.run(DAY, collect=False, push=False)
    second = runner.run(DAY, collect=False, push=False)

    assert first["status"] == second["status"] == "succeeded"
    assert client.calls == Counter({role: 1 for role in ROLE_NAMES.values()})
    assert len(postprocess_calls) == 2
    assert (runner.team / "outputs" / DAY / "generation.json").is_file()
    assert runner.ledger.get_run(second["run_id"])["steps"][0]["status"] == "succeeded"
    plan = json.loads((runner.team / "outputs" / DAY / "plan.json").read_text())
    assert plan["risk_contract"] == "a_share_risk.v1"
    assert plan["risk_reviews"][0]["reason_codes"] == []


@pytest.mark.parametrize("damage", [None, "role", "risk", "ack", "postprocess", "latest_failed", "ledger"])
def test_fallback_checks_real_generation_without_repairing_or_regenerating(meeting, monkeypatch, damage):
    from trading_team import kimi_fallback
    runner, client, _ = meeting
    run = runner.run(DAY, collect=False, push=False)
    monkeypatch.setattr(kimi_fallback, "TEAM", runner.team)
    monkeypatch.setattr(kimi_fallback, "latest_scheduled_run", lambda day: {"runId": "legacy-primary", "status": "succeeded"})
    def forbidden(*args, **kwargs):
        pytest.fail("inspection must not call a model, load credentials, or postprocess")
    monkeypatch.setattr(kimi_fallback, "load_local_env", forbidden)
    monkeypatch.setattr(kimi_fallback, "KimiClient", forbidden)
    output = runner.team / "outputs" / DAY
    if damage == "role":
        (output / "01_fundamental.md").write_text("changed")
    elif damage == "risk":
        path = output / "plan.json"
        plan = json.loads(path.read_text())
        plan.pop("risk_contract")
        path.write_text(json.dumps(plan))
    elif damage == "ack":
        step = runner.ledger.latest_step(run["run_id"], "publish_generation")
        runner.ledger.finish_step(step["id"], status="failed", error="interrupted")
    elif damage == "postprocess":
        runner.ledger.finish_run(run["run_id"], status="failed", error="delivery unknown")
    elif damage == "latest_failed":
        later = runner.ledger.start_run("daily_research", DAY, trigger_source="fixture", resume=False)
        runner.ledger.finish_run(later, status="failed", error="finalization interrupted")
    elif damage == "ledger":
        runner.ledger.path.unlink()
    def snapshot():
        return {str(p.relative_to(runner.team)): p.read_bytes() for p in runner.team.rglob("*")
                if p.is_file() and p.suffix not in (".db-shm", ".db-wal")}
    before = snapshot()
    result = kimi_fallback.fallback(DAY)
    assert result["status"] == ("skipped" if damage is None else "blocked")
    if damage is None:
        assert result["code"] == "verified_generation"
        assert result["plan_hash"] == canonical_plan_hash(json.loads((output / "plan.json").read_text()))
    assert snapshot() == before
    assert sum(client.calls.values()) == 7


def test_trader_receives_existing_inventory_and_guard_versions(meeting, monkeypatch):
    runner, client, _ = meeting
    inputs = {}
    complete = client.complete
    def record(system, user):
        inputs[system.splitlines()[0]] = user
        return complete(system, user)
    monkeypatch.setattr(client, "complete", record)
    monkeypatch.setattr("trading_team.kimi_daily.dual_paper_snapshot", lambda *args, **kwargs: {
        "portfolios": {"no_gate": {"protection": {"active": {"601318": {"order_hash": "guard-version-test"}}},
                                    "positions": [{"code": "601318", "qty": 200, "available_qty": 100}]}}})
    runner.run(DAY, collect=False, push=False)
    for role in ("trader", "risk_manager"):
        assert "guard-version-test" in inputs[role]
        assert '"available_qty": 100' in inputs[role]


@pytest.mark.parametrize("bad_review", [
    None,
    {"code": "601318", "decision": "否决", "reason": "market_ok=false"},
    {"code": "601318", "decision": "降仓通过", "reason_codes": ["position_limit"],
     "position_pct": .9, "reason": "invalid reduction"},
])
def test_invalid_risk_role_is_failed_not_cached_and_retry_recovers(meeting, monkeypatch, bad_review):
    runner, client, postprocess = meeting
    complete = client.complete
    def faulty(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == "risk_manager":
            if bad_review is None:
                result["daily_conclusion_markdown"] = "invalid conclusion"
            else:
                block = {"role": "risk_manager", "date": DAY, "reviews": [bad_review]}
                result["report_markdown"] = "```json\n" + json.dumps(block) + "\n```"
        return result
    monkeypatch.setattr(client, "complete", faulty)
    with pytest.raises(ValueError, match="risk contract"):
        runner.run(DAY, collect=False, push=False)
    failed_run = runner.ledger.get_run(runner.ledger.list_runs()[0]["run_id"])
    failed_step = runner.ledger.latest_step(failed_run["run_id"], "role_07")
    assert failed_step["status"] == "failed"
    rejected = json.loads((runner.root / failed_step["artifact_path"]).read_text())
    assert rejected["report_markdown"]
    if bad_review is None:
        assert rejected["daily_conclusion_markdown"] == "invalid conclusion"
    assert not postprocess and not (runner.team / "outputs" / DAY / "plan.json").exists()
    monkeypatch.setattr(client, "complete", complete)
    result = runner.run(DAY, collect=False, push=False)
    assert result["status"] == "succeeded"
    assert client.calls["risk_manager"] == 2
    assert all(client.calls[role] == 1 for role in ROLE_NAMES.values() if role != "risk_manager")


@pytest.mark.parametrize("report", [
    "",
    "Technical report without a structured conclusion",
    '```json\n{"role":"technical","date":"2026-09-18","ratings":[]}\n```',
])
def test_invalid_role_preserves_response_request_and_input_for_recovery(meeting, monkeypatch, report):
    from trading_team.kimi_daily import _response_hash, _sha256

    runner, client, postprocess = meeting
    complete = client.complete
    rejected_response = {"report_markdown": report}

    def invalid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == "technical":
            client.last_meta.update(request_id="fixture-invalid-technical", finish_reason="stop",
                                    usage={"total_tokens": 123})
            return rejected_response
        return result

    monkeypatch.setattr(client, "complete", invalid)
    with pytest.raises(ValueError, match="角色 04"):
        runner.run(DAY, collect=False, push=False, publish_only=True)
    run = next(r for r in runner.ledger.list_runs() if r["workflow"] == "daily_research")
    step = runner.ledger.latest_step(run["run_id"], "role_04")
    assert run["status"] == step["status"] == "failed"
    assert step["artifact_path"] is not None
    path = runner.root / step["artifact_path"]
    raw = path.read_bytes()
    assert json.loads(raw) == rejected_response
    meta = json.loads(path.with_suffix(".run.json").read_text())
    inputs = path.with_suffix(".input.txt").read_text()
    assert meta["input_hash"] == _sha256(inputs) == step["input_hash"]
    assert meta["response_hash"] == _response_hash(rejected_response)
    assert meta["request"]["request_id"] == "fixture-invalid-technical"
    assert step["metadata"]["request"] == meta["request"]
    assert "technical_000300.json" in inputs
    assert not postprocess and not (runner.team / "outputs" / DAY).exists()
    assert not (runner.team / ".runs/staging" / DAY / "04_technical.run.json").exists()

    monkeypatch.setattr(client, "complete", complete)
    result = runner.run(DAY, collect=False, push=False, publish_only=True)
    assert result["status"] == "succeeded" and result["run_id"] == run["run_id"]
    assert client.calls["technical"] == 2
    assert all(client.calls[role] == 1 for role in ROLE_NAMES.values() if role != "technical")
    assert path.read_bytes() == raw
    assert [a["status"] for a in runner.ledger.get_run(run["run_id"])["attempts"]] == ["failed", "succeeded"]


def test_multica_retry_preserves_verified_execution_bindings(meeting, monkeypatch):
    runner, client, _ = meeting
    contexts = [{"provider": "multica", "issue_id": "issue-1", "task_id": task,
                 "verification": "remote_verified", "role": "Research Lead"}
                for task in ("task-1", "task-2")]
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context",
                        lambda *_args, **_kwargs: contexts.pop(0))
    client.fail_role = "technical"
    with pytest.raises(RuntimeError, match="interruption"):
        runner.run(DAY, collect=False, push=False, publish_only=True,
                   trigger_source="multica", multica_issue_id="issue-1")
    result = runner.run(DAY, collect=False, push=False, publish_only=True,
                        trigger_source="multica", multica_issue_id="issue-1")
    run = runner.ledger.get_run(result["run_id"])
    assert [a["external_context"]["task_id"] for a in run["attempts"]] == ["task-1", "task-2"]
    assert run["attempts"][0]["status"] == "failed"
    assert run["attempts"][1]["status"] == "succeeded"


def test_unverified_multica_execution_fails_before_any_model_call(meeting, monkeypatch):
    runner, client, _ = meeting

    def reject(*_args, **_kwargs):
        raise ValueError("Multica identity mismatch")

    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context", reject)
    with pytest.raises(ValueError, match="identity"):
        runner.run(DAY, collect=False, push=False, trigger_source="multica", multica_issue_id="issue-1")
    assert not client.calls
    run = runner.ledger.get_run(runner.ledger.list_runs()[0]["run_id"])
    assert run["status"] == "failed"
    assert run["steps"][0]["step_name"] == "verify_multica_context"
    assert run["attempts"][0]["external_context"] == {}


def test_multica_holiday_cannot_publish_even_if_agent_runs_command(meeting, monkeypatch, finalize_calendar):
    runner, client, postprocess = meeting
    holiday = "2026-09-25"
    context = {"provider": "multica", "verification": "remote_verified", "issue_id": "issue-1",
               "task_id": "task-1", "role": "Research Lead"}
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context",
                        lambda *_args, **_kwargs: dict(context))
    monkeypatch.setattr("trading_team.kimi_daily.DEFAULT_TRADING_CALENDAR", finalize_calendar, raising=False)

    with pytest.raises(ValueError, match="trading session"):
        runner.run(holiday, collect=True, push=False, publish_only=True,
                   trigger_source="multica", multica_issue_id="issue-1")

    assert not client.calls and not postprocess
    assert not (runner.team / "context" / holiday).exists()
    assert not (runner.team / "outputs" / holiday).exists()
    run = runner.ledger.get_run(runner.ledger.latest_run("daily_research", holiday)["run_id"])
    assert run["status"] == "failed"
    assert run["steps"][0]["step_name"] == "trading_session_gate"


@pytest.mark.parametrize("push", [False, True])
def test_multica_cannot_bypass_independent_review_with_full_run(meeting, monkeypatch, finalize_calendar, push):
    runner, client, postprocess = meeting
    remote = {"provider": "multica", "verification": "remote_verified", "issue_id": "issue-1",
              "task_id": "task-1", "role": "Research Lead"}
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context",
                        lambda *_args, **_kwargs: dict(remote))
    monkeypatch.setattr("trading_team.kimi_daily.DEFAULT_TRADING_CALENDAR", finalize_calendar)

    with pytest.raises(ValueError, match="publication-only scope"):
        runner.run(DAY, collect=False, push=push, publish_only=False,
                   trigger_source="multica", multica_issue_id="issue-1")

    assert not client.calls and not postprocess
    assert not (runner.team / "outputs" / DAY).exists()
    run = runner.ledger.get_run(runner.ledger.latest_run("daily_research", DAY)["run_id"])
    assert run["status"] == "failed"
    assert run["steps"][0]["step_name"] == "publication_scope_gate"
    assert run["attempts"][0]["external_context"] == remote


def test_multica_cannot_force_replace_existing_publication(meeting, monkeypatch, finalize_calendar):
    runner, client, _ = meeting
    contexts = [{"provider": "multica", "verification": "remote_verified", "issue_id": "issue-1",
                 "task_id": task, "role": "Research Lead"} for task in ("task-1", "task-2")]
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context",
                        lambda *_args, **_kwargs: contexts.pop(0))
    monkeypatch.setattr("trading_team.kimi_daily.DEFAULT_TRADING_CALENDAR", finalize_calendar)
    runner.run(DAY, collect=False, push=False, publish_only=True,
               trigger_source="multica", multica_issue_id="issue-1")
    published = (runner.team / "outputs" / DAY / "generation.json").read_bytes()
    calls = client.calls.copy()

    with pytest.raises(ValueError, match="publication-only scope"):
        runner.run(DAY, collect=False, force=True, push=False, publish_only=True,
                   trigger_source="multica", multica_issue_id="issue-1")

    assert client.calls == calls
    assert (runner.team / "outputs" / DAY / "generation.json").read_bytes() == published
    run = runner.ledger.get_run(runner.ledger.latest_run("daily_research", DAY)["run_id"])
    assert run["status"] == "failed"
    assert run["steps"][0]["step_name"] == "publication_scope_gate"


def test_multica_publication_rejects_stale_complete_archive_before_model(
    meeting, monkeypatch, execution_cache, finalize_calendar,
):
    from trading_team.market_archive import capture_closing

    runner, client, _ = meeting
    np.save(finalize_calendar, np.array(sorted({*np.load(finalize_calendar), 20260917})))
    bar = {"open": 54, "high": 55, "low": 53, "close": 54, "prev_close": 54}
    execution_cache(runner.team, "2026-09-17", {code: bar for code in ("601318", "000300")})
    receipt = capture_closing("2026-09-18", team=runner.team, calendar_path=finalize_calendar,
                              now=datetime(2026, 9, 18, 8))
    assert receipt["date"] == "2026-09-17"
    context = runner.team / "context" / DAY
    manifest_path = context / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["latest_trade_date"] = "2026-09-17"
    manifest["execution_archive"] = receipt
    manifest_path.write_text(json.dumps(manifest))
    (context / "execution_archive.json").write_text(json.dumps(receipt))
    remote = {"provider": "multica", "verification": "remote_verified", "issue_id": "issue-1",
              "task_id": "task-1", "role": "Research Lead"}
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context",
                        lambda *_args, **_kwargs: dict(remote))
    monkeypatch.setattr("trading_team.kimi_daily.DEFAULT_TRADING_CALENDAR", finalize_calendar)

    with pytest.raises(ValueError, match="previous completed session"):
        runner.run(DAY, collect=False, push=False, publish_only=True,
                   trigger_source="multica", multica_issue_id="issue-1")

    assert not client.calls
    assert not (runner.team / "outputs" / DAY).exists()


def test_multica_reuse_rejects_stale_published_archive(
    meeting, monkeypatch, execution_cache, finalize_calendar,
):
    from trading_team.market_archive import capture_closing

    runner, client, _ = meeting
    np.save(finalize_calendar, np.array(sorted({*np.load(finalize_calendar), 20260917})))
    bar = {"open": 54, "high": 55, "low": 53, "close": 54, "prev_close": 54}
    execution_cache(runner.team, "2026-09-17", {code: bar for code in ("601318", "000300")})
    receipt = capture_closing("2026-09-18", team=runner.team, calendar_path=finalize_calendar,
                              now=datetime(2026, 9, 18, 8))
    assert receipt["date"] == "2026-09-17"
    context = runner.team / "context" / DAY
    manifest_path = context / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["latest_trade_date"] = "2026-09-17"
    manifest["execution_archive"] = receipt
    manifest_path.write_text(json.dumps(manifest))
    (context / "execution_archive.json").write_text(json.dumps(receipt))
    runner.run(DAY, collect=False, push=False, publish_only=True)
    published = (runner.team / "outputs" / DAY / "generation.json").read_bytes()
    calls = client.calls.copy()
    remote = {"provider": "multica", "verification": "remote_verified", "issue_id": "issue-1",
              "task_id": "task-1", "role": "Research Lead"}
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context",
                        lambda *_args, **_kwargs: dict(remote))
    monkeypatch.setattr("trading_team.kimi_daily.DEFAULT_TRADING_CALENDAR", finalize_calendar)

    with pytest.raises(ValueError, match="previous completed session"):
        runner.run(DAY, collect=False, push=False, publish_only=True,
                   trigger_source="multica", multica_issue_id="issue-1")

    assert client.calls == calls
    assert (runner.team / "outputs" / DAY / "generation.json").read_bytes() == published


def test_multica_reuse_rejects_manual_publisher(meeting, monkeypatch, finalize_calendar):
    runner, client, _ = meeting
    runner.run(DAY, collect=False, push=False, publish_only=True)
    published = (runner.team / "outputs" / DAY / "generation.json").read_bytes()
    calls = client.calls.copy()
    remote = {"provider": "multica", "verification": "remote_verified", "issue_id": "issue-1",
              "task_id": "task-1", "role": "Research Lead"}
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context",
                        lambda *_args, **_kwargs: dict(remote))
    monkeypatch.setattr("trading_team.kimi_daily.DEFAULT_TRADING_CALENDAR", finalize_calendar)

    with pytest.raises(ValueError, match="verified Multica publisher"):
        runner.run(DAY, collect=False, push=False, publish_only=True,
                   trigger_source="multica", multica_issue_id="issue-1")

    assert client.calls == calls
    assert (runner.team / "outputs" / DAY / "generation.json").read_bytes() == published


def test_multica_reuse_accepts_verified_original_publisher(meeting, monkeypatch, finalize_calendar):
    runner, client, _ = meeting
    contexts = [{"provider": "multica", "verification": "remote_verified", "issue_id": "issue-1",
                 "task_id": task, "role": "Research Lead"} for task in ("task-1", "task-2")]
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context",
                        lambda *_args, **_kwargs: contexts.pop(0))
    monkeypatch.setattr("trading_team.kimi_daily.DEFAULT_TRADING_CALENDAR", finalize_calendar)
    first = runner.run(DAY, collect=False, push=False, publish_only=True,
                       trigger_source="multica", multica_issue_id="issue-1")
    calls = client.calls.copy()

    second = runner.run(DAY, collect=False, push=False, publish_only=True,
                        trigger_source="multica", multica_issue_id="issue-1")

    assert first["status"] == second["status"] == "succeeded"
    assert first["plan_hash"] == second["plan_hash"]
    assert client.calls == calls


@pytest.mark.parametrize("role", ["Data Quality", "Market Analysts", "Strategy Research", "Paper Trader",
                                  "Risk Manager", "Audit Verifier", None])
def test_only_research_lead_can_start_multica_publication(meeting, monkeypatch, role):
    runner, client, postprocess = meeting
    context = {"provider": "multica", "verification": "remote_verified", "issue_id": "issue-1",
               "task_id": "task-1", "role": role}
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context",
                        lambda *_args, **_kwargs: dict(context))
    with pytest.raises(ValueError, match="Research Lead"):
        runner.run(DAY, collect=False, push=False, force=True,
                   trigger_source="multica", multica_issue_id="issue-1")
    assert not client.calls and not postprocess
    assert not (runner.team / "outputs" / DAY).exists()
    assert not (runner.team / ".runs/staging" / DAY).exists()
    run = runner.ledger.get_run(runner.ledger.list_runs()[0]["run_id"])
    assert run["status"] == "failed"
    assert run["attempts"][0]["external_context"] == context
    assert run["steps"][0]["step_name"] == "verify_multica_context"


@pytest.mark.parametrize("identity_key", ["MULTICA_TASK_ID", "MULTICA_AGENT_ID", "MULTICA_WORKSPACE_ID"])
def test_partial_multica_identity_cannot_fall_back_to_manual_publication(meeting, monkeypatch, identity_key):
    runner, client, postprocess = meeting
    for key in ("MULTICA_TASK_ID", "MULTICA_AGENT_ID", "MULTICA_WORKSPACE_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(identity_key, "00000000-0000-4000-8000-000000000001")
    with pytest.raises(ValueError, match="Multica"):
        runner.run(DAY, collect=False, push=False, trigger_source="manual")
    assert not client.calls and not postprocess
    run = runner.ledger.get_run(runner.ledger.list_runs()[0]["run_id"])
    assert run["status"] == "failed"
    assert run["steps"][0]["step_name"] == "verify_multica_context"


@pytest.mark.parametrize("relative_path", [
    f"context/{DAY}/fundamental_601318.json",
    "prompts/01_fundamental.md",
    f"outputs/{DAY}/01_fundamental.md",
    f"outputs/{DAY}/06_trader.run.json",
    f"outputs/{DAY}/plan.json",
    f"outputs/{DAY}/daily_conclusion.md",
])
def test_published_drift_is_rejected_before_postprocess(meeting, relative_path):
    runner, client, postprocess_calls = meeting
    runner.run(DAY, collect=False, push=False)
    path = runner.team / relative_path
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        if path.name == "plan.json":
            data["proposals"][0]["entry"] = [52]
        else:
            data["changed"] = True
        path.write_text(json.dumps(data))
    else:
        path.write_text(path.read_text() + "\nChanged")

    with pytest.raises(ValueError, match="generation"):
        runner.run(DAY, collect=False, push=False)

    assert len(postprocess_calls) == 1
    assert sum(client.calls.values()) == 7
    failed = runner.ledger.get_run(runner.ledger.list_runs()[0]["run_id"])
    assert failed["status"] == "failed"
    assert failed["steps"][0]["status"] == "failed"


def test_legacy_report_requires_generation_evidence(meeting):
    runner, _, postprocess_calls = meeting
    output = runner.team / "outputs" / DAY
    output.mkdir(parents=True)
    (output / "daily_conclusion.md").write_text("legacy")
    with pytest.raises(ValueError, match="generation"):
        runner.run(DAY, collect=False, push=False)
    assert postprocess_calls == []


def test_missing_role_is_not_a_completed_generation(meeting):
    runner, _, postprocess_calls = meeting
    runner.run(DAY, collect=False, push=False)
    (runner.team / "outputs" / DAY / "04_technical.md").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        runner.run(DAY, collect=False, push=False)
    assert len(postprocess_calls) == 1
    assert runner.ledger.list_runs()[0]["status"] == "failed"


def test_reuse_failure_is_terminal_and_retry_records_new_attempt(meeting, monkeypatch):
    runner, client, _ = meeting
    runner.run(DAY, collect=False, push=False)
    real_postprocess = runner._postprocess

    def fail(*_args):
        raise RuntimeError("injected finalization interruption")

    monkeypatch.setattr(runner, "_postprocess", fail)
    with pytest.raises(RuntimeError, match="finalization interruption"):
        runner.run(DAY, collect=False, push=False)
    failed = runner.ledger.list_runs()[0]
    assert failed["status"] == "failed"
    monkeypatch.setattr(runner, "_postprocess", real_postprocess)
    result = runner.run(DAY, collect=False, push=False)
    assert result["run_id"] == failed["run_id"]
    assert runner.ledger.get_run(result["run_id"])["attempt"] == 2
    assert sum(client.calls.values()) == 7


@pytest.mark.parametrize("artifact", ["report", "response"])
def test_interrupted_role_cache_detects_output_corruption(meeting, artifact):
    runner, client, _ = meeting
    client.fail_role = "risk_manager"
    with pytest.raises(RuntimeError, match="model interruption"):
        runner.run(DAY, collect=False, push=False)
    stage = runner.team / ".runs/staging" / DAY
    if artifact == "report":
        path = stage / "01_fundamental.md"
        path.write_text(path.read_text().replace("Report", "Altered"))
    else:
        path = stage / "06_trader.run.json"
        data = json.loads(path.read_text())
        data["response"]["proposals"][0]["entry"] = [1]
        path.write_text(json.dumps(data))
    runner.run(DAY, collect=False, push=False)
    assert client.calls["fundamental" if artifact == "report" else "trader"] == 2
    plan = json.loads((runner.team / "outputs" / DAY / "plan.json").read_text())
    assert plan["proposals"][0]["entry"] == [53.5]


def test_reused_run_records_materialized_current_plan_hash(meeting):
    runner, _, _ = meeting
    runner.run(DAY, collect=False, push=False)
    path = runner.team / "decisions" / DAY / "decision.json"
    decision = json.loads(path.read_text())
    decision["plan_hash"] = "stale-derived-state"
    path.write_text(json.dumps(decision))
    result = runner.run(DAY, collect=False, push=False)
    plan = json.loads((runner.team / "outputs" / DAY / "plan.json").read_text())
    assert runner.ledger.get_run(result["run_id"])["plan_hash"] == canonical_plan_hash(plan)


def test_system_steps_recheck_mutable_state_during_retry(meeting):
    runner, _, _ = meeting
    run_id = runner.ledger.start_run("daily_research", DAY, trigger_source="test")
    state = {"plan_hash": "v1"}
    assert runner._run_system_step(run_id, "build_index", lambda: dict(state)) == state
    state["plan_hash"] = "v2"
    assert runner._run_system_step(run_id, "build_index", lambda: dict(state)) == state


def test_receipt_edits_cannot_self_certify_changed_reports(meeting):
    runner, _, postprocess_calls = meeting
    runner.run(DAY, collect=False, push=False)
    output = runner.team / "outputs" / DAY
    receipt_path = output / "generation.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["model"] = "forged-model"
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="ledger record"):
        runner.run(DAY, collect=False, push=False)
    assert len(postprocess_calls) == 1


def test_publish_recovers_after_directory_rename_before_acknowledgement(meeting, monkeypatch):
    runner, client, _ = meeting
    finish = runner.ledger.finish_step

    def interrupted(step_id, **kwargs):
        if kwargs.get("artifact_path", "").endswith("generation.json"):
            raise RuntimeError("injected publish acknowledgement interruption")
        return finish(step_id, **kwargs)

    monkeypatch.setattr(runner.ledger, "finish_step", interrupted)
    with pytest.raises(RuntimeError, match="publish acknowledgement"):
        runner.run(DAY, collect=False, push=False)
    failed = runner.ledger.list_runs()[0]
    assert failed["status"] == "failed"
    monkeypatch.setattr(runner.ledger, "finish_step", finish)
    result = runner.run(DAY, collect=False, push=False)
    assert result["run_id"] == failed["run_id"]
    assert sum(client.calls.values()) == 7
    assert runner.ledger.latest_step(result["run_id"], "publish_generation")["metadata"] == {
        "recovered_after_publish": True,
    }


def test_force_archives_previous_generation_and_interrupted_stage(meeting):
    runner, client, _ = meeting
    first = runner.run(DAY, collect=False, push=False)
    original = (runner.team / "outputs" / DAY / "generation.json").read_text()
    stage = runner.team / ".runs/staging" / DAY
    stage.mkdir(parents=True)
    (stage / "partial.md").write_text("interrupted attempt")

    second = runner.run(DAY, collect=False, force=True, push=False)
    archive = runner.team / ".runs/archive" / DAY / second["run_id"]
    assert (archive / "outputs/generation.json").read_text() == original
    assert (archive / "staging/partial.md").read_text() == "interrupted attempt"
    assert sum(client.calls.values()) == 14
    assert first["run_id"] != second["run_id"]


def test_same_day_concurrent_trigger_cannot_take_over_running_work(meeting):
    runner, client, _ = meeting
    path = runner.team / ".runs/locks" / f"daily_research-{DAY}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    before = runner.ledger.list_runs()
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            runner.run(DAY, collect=False, push=False)
    assert runner.ledger.list_runs() == before
    assert sum(client.calls.values()) == 0
    assert runner.run(DAY, collect=False, push=False)["status"] == "succeeded"


def test_real_postprocess_accepts_generated_bundle_and_reuses_it(meeting, monkeypatch):
    from trading_team.daily_finalize import finalize_available_tracked

    runner, client, _ = meeting
    loops = runner.team / "loops"
    loops.mkdir()
    (loops / "accuracy.json").write_text('{"entries":[]}')
    monkeypatch.setattr(runner, "_postprocess", DailyMeetingRunner._postprocess.__get__(runner))
    monkeypatch.setattr(
        "trading_team.daily_finalize.finalize_available_tracked",
        lambda **kwargs: finalize_available_tracked(today=date.fromisoformat(DAY), **kwargs),
    )
    first = runner.run(DAY, collect=False, push=False)
    second = runner.run(DAY, collect=False, push=False)
    assert first["status"] == second["status"] == "succeeded"
    assert next(item for item in second["finalize"] if item["date"] == DAY)["status"] == "reused"
    decision_dir = runner.team / "decisions" / DAY
    decision = json.loads((decision_dir / "decision.json").read_text())
    paper_decision = json.loads((decision_dir / "paper_decision.json").read_text())
    assert decision["plan_hash"] == paper_decision["plan_hash"]
    assert (runner.team / "outputs/index.json").is_file()
    assert sum(client.calls.values()) == 7


@pytest.mark.parametrize("filename", ["decision.json", "paper_decision.json"])
def test_stale_derived_version_cannot_reach_notification(meeting, monkeypatch, filename):
    runner, _, _ = meeting
    runner.run(DAY, collect=False, push=False)
    path = runner.team / "decisions" / DAY / filename
    decision = json.loads(path.read_text())
    decision["plan_hash"] = "stale-version"
    path.write_text(json.dumps(decision))
    monkeypatch.setattr(runner, "_postprocess", DailyMeetingRunner._postprocess.__get__(runner))
    monkeypatch.setattr("trading_team.kimi_daily.enrich_plan", lambda *_args: {})
    monkeypatch.setattr("trading_team.kimi_daily.backfill_accuracy", lambda *_args: {})
    monkeypatch.setattr(
        "trading_team.daily_finalize.finalize_available_tracked",
        lambda **kwargs: [{"date": DAY, "status": "reused"}],
    )
    sent = []
    monkeypatch.setattr("trading_team.push_feishu.push", lambda *args, **kwargs: sent.append(args))
    with pytest.raises(ValueError, match="plan_hash"):
        runner.run(DAY, collect=False, push=True)
    assert sent == []
    latest = runner.ledger.list_runs()[0]
    assert latest["status"] == "failed"
    assert runner.ledger.latest_step(latest["run_id"], "verify_materialization")["status"] == "failed"


def test_readonly_generation_inspection_does_not_repair_acknowledgement(meeting, finalize_calendar):
    from trading_team.operational_snapshot import operational_snapshot

    runner, _, _ = meeting
    run = runner.run(DAY, collect=False, push=False)
    kwargs = {"team": runner.team, "calendar_path": finalize_calendar}
    assert operational_snapshot(**kwargs)["research"]["generation"]["status"] == "verified"
    publisher = runner.ledger.latest_step(run["run_id"], "publish_generation")
    runner.ledger.finish_step(publisher["id"], status="failed", error="interrupted acknowledgement")
    before = runner.ledger.get_run(run["run_id"])
    assert operational_snapshot(**kwargs)["research"]["generation"]["status"] == "recovery_required"
    assert runner.ledger.get_run(run["run_id"]) == before
    (runner.team / "outputs" / DAY / "daily_conclusion.md").write_text("changed report")
    assert operational_snapshot(**kwargs)["research"]["generation"]["status"] == "invalid"


def test_real_push_unknown_blocks_retries_without_repeating_roles(meeting, monkeypatch, stub_research_reviews,
                                                                   execution_cache):
    from trading_team import push_feishu, notification_outbox
    from trading_team import daily_finalize
    runner, client, _ = meeting
    execution_cache(runner.team, DAY, {code: {"open": 53, "high": 54, "low": 52,
                                             "close": 53.5, "prev_close": 54}
                                       for code in ("601318", "000300")})
    loops = runner.team / "loops"
    loops.mkdir()
    (loops / "accuracy.json").write_text('{"entries":[]}')
    monkeypatch.setattr(runner, "_postprocess", DailyMeetingRunner._postprocess.__get__(runner))
    finalize_available_tracked = daily_finalize.finalize_available_tracked
    monkeypatch.setattr("trading_team.daily_finalize.finalize_available_tracked",
                        lambda **kwargs: finalize_available_tracked(
                            now=datetime(2026, 9, 22, 8, tzinfo=daily_finalize.SHANGHAI), **kwargs))
    monkeypatch.setattr(push_feishu, "_config", lambda: {"app_id":"fixture", "app_secret":"unused",
                                                       "receive_id":"fake", "receive_id_type":"chat_id"})
    sends = []
    def send(*args):
        sends.append(1)
        raise RuntimeError("response lost after isolated send")
    monkeypatch.setattr(push_feishu, "_send", send)
    for _ in range(2):
        with pytest.raises(notification_outbox.DeliveryUnknown):
            runner.run(DAY, collect=False, push=True)
    assert sends == [1] and sum(client.calls.values()) == 7
    run = next(item for item in runner.ledger.list_runs() if item["workflow"] == "daily_research")
    assert run["status"] == "failed"
    assert runner.ledger.latest_step(run["run_id"], "push")["status"] == "failed"
    item = notification_outbox.summary(team=runner.team)["issues"][0]
    notification_outbox.resolve(DAY, item["plan_hash"], team=runner.team, expected_hash=item["record_hash"],
                                attempt_id=item["attempt_id"], outcome="delivered", operator="offline",
                                evidence="fixture message observed", message_id="om_fixture")
    result = runner.run(DAY, collect=False, push=True)
    assert result["status"] == "succeeded" and result["notification"]["status"] == "skipped"
    assert sends == [1] and sum(client.calls.values()) == 7


def test_publish_only_hands_off_without_decisions_paper_or_notification(meeting, monkeypatch):
    from trading_team.kimi_daily import inspect_generation
    runner, client, _ = meeting
    monkeypatch.setattr(runner, "_postprocess", lambda *args: pytest.fail("publication must not finalize"))
    for _ in range(2):
        result = runner.run(DAY, collect=False, push=False, publish_only=True)
        assert result["scope"] == "publication_only"
        assert result["business_complete"] is False
        assert result["finalize"]["status"] == "not_requested"
        assert result["notification"]["status"] == "disabled"
        proof = inspect_generation(runner.team, DAY, runner.ledger)
        assert runner.ledger.get_run(result["run_id"])["plan_hash"] == proof["plan_hash"]
        assert result["plan_hash"] == proof["plan_hash"]
    assert sum(client.calls.values()) == 7
    assert not (runner.team / "decisions").exists()
    assert not (runner.root / "paper").exists()


def test_publish_only_can_recover_a_published_bundle_after_finalization_failure(meeting, monkeypatch):
    runner, client, _ = meeting
    monkeypatch.setattr(runner, "_postprocess", DailyMeetingRunner._postprocess.__get__(runner))
    (runner.team / "loops").mkdir()
    (runner.team / "loops/accuracy.json").write_text('{"entries":[]}')
    with pytest.raises(RuntimeError, match="daily_finalize failed"):
        runner.run(DAY, collect=False, push=False)
    failed = next(r for r in runner.ledger.list_runs() if r["workflow"] == "daily_research")
    original = (runner.team / "outputs" / DAY / "generation.json").read_bytes()
    result = runner.run(DAY, collect=False, push=False, publish_only=True)
    assert result["run_id"] == failed["run_id"]
    attempts = runner.ledger.get_run(result["run_id"])["attempts"]
    assert [a["status"] for a in attempts] == ["failed", "succeeded"]
    assert result["business_complete"] is False
    assert (runner.team / "outputs" / DAY / "generation.json").read_bytes() == original
    assert sum(client.calls.values()) == 7
    assert not (runner.root / "paper").exists()


def test_publish_only_does_not_turn_into_paper_authorization(meeting):
    from trading_team.daily_finalize import run_finalize_workflow
    runner, client, _ = meeting
    runner.run(DAY, collect=False, push=False, publish_only=True)
    with pytest.raises(ValueError, match="review gate blocked"):
        run_finalize_workflow(DAY, team=runner.team, replay=False, paper=True)
    assert not (runner.root / "paper").exists()
    assert sum(client.calls.values()) == 7


def test_publish_only_rejects_push_before_running_roles(meeting):
    runner, client, _ = meeting
    before = runner.ledger.list_runs()
    with pytest.raises(ValueError, match="publish_only requires push=False"):
        runner.run(DAY, collect=False, push=True, publish_only=True)
    assert not client.calls
    assert runner.ledger.list_runs() == before
