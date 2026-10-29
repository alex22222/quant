import importlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from test_daily_research_recovery import DAY, meeting
from trading_team.multica_adapter import MulticaCLI
from trading_team.run_ledger import RunLedger


@pytest.fixture
def reviews(meeting, monkeypatch):
    module = importlib.import_module("trading_team.multica_reviews")
    runner, client, _ = meeting
    runner.run(DAY, collect=False, push=False)
    state = {"project_id": str(uuid4()), "squad_id": str(uuid4()),
             "workspace_id": str(uuid4()),
             "agent_ids": {name: str(uuid4()) for name in
                           ["Research Lead", *[r["name"] for r in module.ROLES.values()]]}}
    path = runner.team / ".runs/multica/bootstrap.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state))
    return module, runner, client, state


def response(module, bundle, verdict="accept"):
    evidence = next(iter(bundle["files"]))
    return {"schema": "multica_review_response.v1", "bundle_hash": bundle["bundle_hash"],
            "role": bundle["role"], "verdict": verdict, "summary": "Offline review fixture.",
            "checks": [{"name": name, "result": "pass" if verdict == "accept" else "unknown",
                        "reason": "Checked archived fixture.", "evidence": [evidence]}
                       for name in module.ROLES[bundle["role"]]["checks"]]}


def test_future_review_title_is_chinese_and_legacy_title_is_stable():
    module = importlib.import_module("trading_team.multica_reviews")
    bundle = {"role": "data-quality", "bundle_hash": "a" * 64, "date": "2026-09-24"}
    assert module.issue_title(bundle) == f"A股审阅 数据质量 {'a' * 64} 2026-09-24"
    bundle["date"] = "2026-09-23"
    assert module.issue_title(bundle) == f"A-share review data-quality {'a' * 64} 2026-09-23"


def test_active_paper_cohort_is_sealed_in_review_source(meeting):
    module = importlib.import_module("trading_team.multica_reviews")
    runner, client, _ = meeting
    config = runner.team.parent / "paper/active_cohort.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "schema": "paper_cohort.v1", "cohort": "2026-09-28-forward",
        "start_date": "2026-09-28", "initial_cash": 100000,
        "accounts": {
            "gate": "paper/cohort_2026-09-28/llm_gate.db",
            "no_gate": "paper/cohort_2026-09-28/llm_no_gate.db",
        },
    }))
    runner.run(DAY, collect=False, push=False)
    state = {"project_id": str(uuid4()), "squad_id": str(uuid4()), "workspace_id": str(uuid4()),
             "agent_ids": {name: str(uuid4()) for name in
                           ["Research Lead", *[role["name"] for role in module.ROLES.values()]]}}
    path = runner.team / ".runs/multica/bootstrap.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state))
    reviews = (module, runner, client, state)
    for role in module.ROLES:
        bundle = module.prepare_review(DAY, role, team=runner.team)
        assert bundle["files"]["paper/active_cohort.json"]["text"] == config.read_text()
        submit(reviews, bundle)
    assert module.review_status(DAY, team=runner.team)["status"] == "accepted"
    config.write_text(config.read_text() + "\n")
    assert module.review_status(DAY, team=runner.team)["status"] == "source_unverified"


def test_chinese_review_title_can_record_exact_remote_receipt(reviews, monkeypatch):
    module, runner, _, _ = reviews
    monkeypatch.setattr(module, "CHINESE_TASK_TITLES_FROM", DAY)
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    assert module.issue_title(bundle).startswith("A股审阅 数据质量 ")
    result = submit(reviews, bundle)
    assert result["receipt"]["external_context"]["issue_title"] == module.issue_title(bundle)
    assert module.review_status(DAY, team=runner.team)["roles"]["data-quality"]["status"] == "accepted"


def remote(reviews, bundle, *, role=None, task_id=None):
    module, runner, _, state = reviews
    name = role or module.ROLES[bundle["role"]]["name"]
    env = {"MULTICA_TASK_ID": task_id or str(uuid4()), "MULTICA_AGENT_ID": state["agent_ids"][name],
           "MULTICA_WORKSPACE_ID": state["workspace_id"], "MULTICA_TOKEN": "secret-not-for-evidence"}
    issue = {"id": str(uuid4()), "title": module.issue_title(bundle), "project_id": state["project_id"],
             "workspace_id": env["MULTICA_WORKSPACE_ID"]}
    task = {"id": env["MULTICA_TASK_ID"], "agent_id": env["MULTICA_AGENT_ID"], "status": "running"}
    calls = []
    cli = MulticaCLI(binary="offline-multica")
    def query(args):
        calls.append(args)
        if args[:2] == ["issue", "get"]:
            return deepcopy(issue)
        assert args[:2] == ["issue", "runs"]
        return [deepcopy(task)]
    cli._run_json = query
    return cli, env, issue, task, calls


def submit(reviews, bundle, document=None, transport=None):
    module, runner, _, _ = reviews
    cli, env, issue, _, _ = transport or remote(reviews, bundle)
    return module.submit_review(DAY, bundle["role"], bundle["bundle_hash"],
                                document or response(module, bundle), issue["id"],
                                team=runner.team, cli=cli, environ=env)


def test_six_role_handoff_is_bound_and_non_executing(reviews):
    module, runner, client, _ = reviews
    output = runner.team / "outputs" / DAY
    original = {p.name: p.read_bytes() for p in output.iterdir()}
    model_calls = dict(client.calls)
    accepted = []
    for role, definition in module.ROLES.items():
        bundle = module.prepare_review(DAY, role, team=runner.team)
        assert bundle == module.prepare_review(DAY, role, team=runner.team)
        assert list(bundle["upstream"]) == accepted
        assert "secret" not in json.dumps(bundle)
        result = submit(reviews, bundle)
        assert result["receipt"]["external_context"]["role"] == definition["name"]
        assert result["execution_authorized"] is False
        accepted.append(role)
    status = module.review_status(DAY, team=runner.team)
    assert status["status"] == "accepted" and status["execution_authorized"] is False
    assert status["production_complete"] is False
    assert original == {p.name: p.read_bytes() for p in output.iterdir()}
    assert model_calls == dict(client.calls)


def test_cannot_skip_a_stage_or_treat_rejection_as_pass(reviews):
    module, runner, _, _ = reviews
    with pytest.raises(ValueError, match="upstream"):
        module.prepare_review(DAY, "risk-manager", team=runner.team)
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    submit(reviews, bundle, response(module, bundle, "reject"))
    assert module.review_status(DAY, team=runner.team)["roles"]["data-quality"]["status"] == "rejected"
    with pytest.raises(ValueError, match="upstream"):
        module.prepare_review(DAY, "market-analysts", team=runner.team)


@pytest.mark.parametrize("damage", ["bundle", "role", "empty_checks", "unknown_check", "pass_with_unknown",
                                    "empty_evidence", "external_path", "approval", "duplicate_check"])
def test_invalid_review_is_not_an_acceptance(reviews, damage):
    module, runner, _, _ = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    document = response(module, bundle)
    if damage == "bundle": document["bundle_hash"] = "0" * 64
    elif damage == "role": document["role"] = "risk-manager"
    elif damage == "empty_checks": document["checks"] = []
    elif damage == "unknown_check": document["checks"][0]["name"] = "invented"
    elif damage == "pass_with_unknown": document["checks"][0]["result"] = "unknown"
    elif damage == "empty_evidence": document["checks"][0]["evidence"] = []
    elif damage == "external_path": document["checks"][0]["evidence"] = ["../../.env"]
    elif damage == "approval": document["approve_trade"] = True
    else: document["checks"].append(deepcopy(document["checks"][0]))
    with pytest.raises(ValueError, match="review"):
        submit(reviews, bundle, document)
    assert module.review_status(DAY, team=runner.team)["status"] != "accepted"


@pytest.mark.parametrize("damage", ["wrong_role", "wrong_title", "terminal_task", "wrong_project"])
def test_remote_role_and_exact_versioned_task_are_required(reviews, damage):
    module, runner, _, _ = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    transport = remote(reviews, bundle, role="Research Lead" if damage == "wrong_role" else None)
    _, _, issue, task, _ = transport
    if damage == "wrong_title": issue["title"] = f"A-share review data-quality {'0' * 64} {DAY}"
    elif damage == "terminal_task": task["status"] = "completed"
    elif damage == "wrong_project": issue["project_id"] = str(uuid4())
    with pytest.raises(ValueError, match="role|Multica|review"):
        submit(reviews, bundle, transport=transport)
    assert module.review_status(DAY, team=runner.team)["roles"]["data-quality"]["status"] != "accepted"


def test_same_task_retry_is_idempotent_but_cannot_change_its_verdict(reviews):
    module, runner, _, _ = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    transport = remote(reviews, bundle)
    first = submit(reviews, bundle, transport=transport)
    before = runner.ledger.list_runs()
    second = submit(reviews, bundle, transport=transport)
    assert first["receipt"] == second["receipt"] and second["reused"] is True
    assert before == runner.ledger.list_runs()
    with pytest.raises(ValueError, match="conflict"):
        submit(reviews, bundle, response(module, bundle, "reject"), transport)


def test_new_rejection_invalidates_downstream_and_old_retry_cannot_reopen(reviews):
    module, runner, _, _ = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    old = remote(reviews, bundle)
    submit(reviews, bundle, transport=old)
    downstream = module.prepare_review(DAY, "market-analysts", team=runner.team)
    submit(reviews, downstream)
    submit(reviews, bundle, response(module, bundle, "reject"))
    retried = submit(reviews, bundle, transport=old)
    assert retried["is_latest"] is False
    status = module.review_status(DAY, team=runner.team)
    assert status["roles"]["data-quality"]["status"] == "rejected"
    assert status["roles"]["market-analysts"]["status"] == "blocked"
    with pytest.raises(ValueError, match="upstream"):
        submit(reviews, downstream)


@pytest.mark.parametrize("changed", ["plan", "report", "binding", "bundle"])
def test_changed_source_cannot_receive_stale_review(reviews, changed):
    module, runner, _, state = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    if changed == "plan":
        path = runner.team / "outputs" / DAY / "plan.json"
        document = json.loads(path.read_text()); document["note"] = "changed"
    elif changed == "report":
        path = runner.team / "outputs" / DAY / "01_fundamental.md"
        path.write_text("changed")
    elif changed == "binding":
        path = runner.team / ".runs/multica/bootstrap.json"
        document = deepcopy(state); document["agent_ids"]["Data Quality"] = str(uuid4())
    else:
        path = module.bundle_path(runner.team, DAY, bundle["bundle_hash"])
        document = deepcopy(bundle); document["source"]["plan_hash"] = "0" * 64
    if changed != "report": path.write_text(json.dumps(document))
    with pytest.raises((ValueError, RuntimeError), match="generation|bundle|review|source"):
        submit(reviews, bundle)
    assert module.review_status(DAY, team=runner.team)["status"] != "accepted"


def test_legacy_generation_and_duplicate_agent_binding_cannot_prepare(reviews):
    module, runner, _, state = reviews
    path = runner.team / ".runs/multica/bootstrap.json"
    state["agent_ids"]["Data Quality"] = state["agent_ids"]["Research Lead"]
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="binding"):
        module.prepare_review(DAY, "data-quality", team=runner.team)
    (runner.team / "outputs" / DAY / "generation.json").unlink()
    assert module.review_status(DAY, team=runner.team)["status"] == "source_unverified"


def test_changed_prompt_keeps_prior_rejection_visible_without_reopening_gate(reviews):
    module, runner, _, _ = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    receipt = submit(reviews, bundle, response(module, bundle, "reject"))["receipt"]
    (runner.team / "prompts/02_sentiment.md").write_text("revised charter")

    status = module.review_status(DAY, team=runner.team)
    assert status["status"] == "source_unverified"
    assert status["roles"] == {}
    assert status["recorded"]["data-quality"] == {
        "status": "rejected", "run_id": receipt["run_id"],
        "receipt_hash": receipt["receipt_hash"], "bundle_hash": bundle["bundle_hash"],
    }
    with pytest.raises(module.ReviewBlocked):
        module.require_reviews(DAY, bundle["source"]["plan_hash"], team=runner.team)


def test_damaged_old_bundle_is_not_a_recorded_verdict(reviews):
    module, runner, _, _ = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    receipt = submit(reviews, bundle)["receipt"]
    (runner.team / "prompts/02_sentiment.md").write_text("revised charter")
    module.bundle_path(runner.team, DAY, bundle["bundle_hash"]).write_text("{}")

    status = module.review_status(DAY, team=runner.team)
    assert status["status"] == "source_unverified"
    assert status["recorded"]["data-quality"] == {"status": "invalid", "run_id": receipt["run_id"]}


def test_review_evidence_survives_whole_workflow_restore(reviews, tmp_path):
    from trading_team.workflow_recovery import backup_workflow, restore_workflow
    module, runner, _, _ = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    submit(reviews, bundle)
    before = module.review_status(DAY, team=runner.team)
    backup = backup_workflow(team=runner.team, target=tmp_path.parent / (tmp_path.name + "-backup"))
    target = tmp_path.parent / (tmp_path.name + "-restore")
    restore_workflow(Path(backup["path"]), backup["manifest_hash"], target)
    restored = target / "trading_team"
    assert module.bundle_path(restored, DAY, bundle["bundle_hash"]).read_bytes() == module.bundle_path(runner.team, DAY, bundle["bundle_hash"]).read_bytes()
    assert module.review_status(DAY, team=restored) == before


def test_status_is_readonly_for_missing_workspace(tmp_path):
    module = importlib.import_module("trading_team.multica_reviews")
    team = tmp_path / "trading_team"
    assert module.review_status(DAY, team=team)["status"] == "source_unverified"
    assert not team.exists()


@pytest.mark.parametrize("damage", ["receipt", "acknowledgement", "bundle_missing", "attempt_missing"])
def test_damaged_review_stays_invalid_instead_of_falling_back(reviews, damage):
    module, runner, _, _ = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    result = submit(reviews, bundle)
    run_id = result["receipt"]["run_id"]
    if damage == "bundle_missing":
        module.bundle_path(runner.team, DAY, bundle["bundle_hash"]).unlink()
    else:
        with runner.ledger._connect() as conn:
            if damage == "acknowledgement":
                conn.execute("DELETE FROM step_runs WHERE run_id=?", (run_id,))
            elif damage == "attempt_missing":
                conn.execute("DELETE FROM workflow_attempts WHERE run_id=?", (run_id,))
            else:
                metadata = runner.ledger.get_run(run_id)["metadata"]
                metadata["receipt"]["response"]["summary"] = "altered"
                conn.execute("UPDATE workflow_runs SET metadata_json=? WHERE run_id=?", (json.dumps(metadata), run_id))
    assert module.review_status(DAY, team=runner.team)["roles"]["data-quality"]["status"] == "invalid"


def test_submit_crash_before_final_ack_resumes_without_repeating_research(reviews, monkeypatch):
    module, runner, client, _ = reviews
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    transport = remote(reviews, bundle)
    complete = RunLedger.finish_run
    def crash(self, run_id, **kwargs):
        if kwargs.get("metadata", {}).get("receipt"):
            raise SystemExit(99)
        return complete(self, run_id, **kwargs)
    calls = dict(client.calls)
    monkeypatch.setattr(RunLedger, "finish_run", crash)
    with pytest.raises(SystemExit): submit(reviews, bundle, transport=transport)
    assert module.review_status(DAY, team=runner.team)["roles"]["data-quality"]["status"] == "incomplete"
    monkeypatch.setattr(RunLedger, "finish_run", complete)
    result = submit(reviews, bundle, transport=transport)
    recorded = runner.ledger.get_run(result["receipt"]["run_id"])
    assert len(recorded["attempts"]) == 2
    assert module.review_status(DAY, team=runner.team)["roles"]["data-quality"]["status"] == "accepted"
    assert dict(client.calls) == calls


def test_temporary_unverified_source_text_cannot_enter_sealed_bundle(reviews, monkeypatch):
    module, runner, _, _ = reviews
    read = Path.read_text
    path = runner.team / "context" / DAY / "fundamental_601318.json"
    original = path.read_text()
    def changed(self, *args, **kwargs):
        # Only the bundle text read changes; the independent generation checks see the real file.
        if self == path and kwargs.get("encoding") == "utf-8":
            import inspect
            if inspect.currentframe().f_back.f_code.co_name == "_source":
                return '{"pe":9999}'
        return read(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", changed)
    with pytest.raises(ValueError, match="source"):
        module.prepare_review(DAY, "data-quality", team=runner.team)
    assert path.read_text() == original


def test_review_workspace_cannot_change_across_handoffs(reviews):
    module, runner, _, _ = reviews
    first = module.prepare_review(DAY, "data-quality", team=runner.team)
    submit(reviews, first)
    second = module.prepare_review(DAY, "market-analysts", team=runner.team)
    transport = remote(reviews, second)
    _, env, issue, _, _ = transport
    env["MULTICA_WORKSPACE_ID"] = issue["workspace_id"] = str(uuid4())
    with pytest.raises(ValueError, match="workspace|binding"):
        submit(reviews, second, transport=transport)


def test_remote_source_cannot_be_published_by_a_checker(reviews):
    module, runner, _, state = reviews
    run = runner.ledger.latest_run("daily_research", DAY)
    context = {"provider": "multica", "verification": "remote_verified", "role": "Risk Manager",
               "agent_id": state["agent_ids"]["Risk Manager"], "task_id": str(uuid4()),
               "project_id": state["project_id"], "workspace_id": state["workspace_id"]}
    with runner.ledger._connect() as conn:
        conn.execute("UPDATE workflow_attempts SET external_context_json=? WHERE run_id=?",
                     (json.dumps(context), run["run_id"]))
    with pytest.raises(ValueError, match="producer"):
        module.prepare_review(DAY, "data-quality", team=runner.team)


@pytest.mark.parametrize("self_review", [False, True])
def test_verified_lead_source_requires_a_distinct_review_task(meeting, monkeypatch, finalize_calendar, self_review):
    module = importlib.import_module("trading_team.multica_reviews")
    runner, client, _ = meeting
    state = {"project_id": str(uuid4()), "squad_id": str(uuid4()), "workspace_id": str(uuid4()),
             "agent_ids": {name: str(uuid4()) for name in
                           ["Research Lead", *[role["name"] for role in module.ROLES.values()]]}}
    path = runner.team / ".runs/multica/bootstrap.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state))
    reviews = (module, runner, client, state)
    bundle = {"role": "data-quality", "bundle_hash": "0" * 64, "date": DAY}
    cli, env, issue, _, _ = remote(reviews, bundle, role="Research Lead")
    issue["title"] = f"A-share daily research {DAY}"
    def verify(day, issue_id):
        return module.verify_execution_context(day, issue_id, cli=cli, environ=env,
            state_path=runner.team / ".runs/multica/bootstrap.json")
    monkeypatch.setattr("trading_team.kimi_daily.verify_execution_context", verify)
    monkeypatch.setattr("trading_team.kimi_daily.DEFAULT_TRADING_CALENDAR", finalize_calendar)
    runner.run(DAY, collect=False, push=False, publish_only=True,
               trigger_source="multica", multica_issue_id=issue["id"])
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    assert bundle["source"]["producer"]["agent_id"] == state["agent_ids"]["Research Lead"]
    transport = remote(reviews, bundle, task_id=env["MULTICA_TASK_ID"] if self_review else None)
    if self_review:
        with pytest.raises(ValueError, match="binding"):
            submit(reviews, bundle, transport=transport)
    else:
        submit(reviews, bundle, transport=transport)
        assert module.review_status(DAY, team=runner.team)["roles"]["data-quality"]["status"] == "accepted"
