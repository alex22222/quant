import importlib.util
import io
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from test_multica_reviews import DAY, meeting, remote, response, reviews, submit
from trading_team import approvals, daily_finalize, execute, paper_pipeline, push_feishu


def accept_all(fixture):
    module, runner, _, _ = fixture
    for role in module.ROLES:
        submit(fixture, module.prepare_review(DAY, role, team=runner.team))


@pytest.fixture
def workspace(reviews, monkeypatch, execution_cache):
    module, runner, _, _ = reviews
    team = runner.team
    monkeypatch.setattr(approvals, "TEAM", team)
    monkeypatch.setattr(approvals, "FILE", team / "loops/approvals.json")
    monkeypatch.setattr(approvals, "DECISIONS_DIR", team / "decisions")
    monkeypatch.setattr(execute, "ROOT", team.parent)
    monkeypatch.setattr(execute, "DECISIONS_DIR", team / "decisions")
    monkeypatch.setattr(execute, "DB", team.parent / "paper/legacy.db")
    monkeypatch.setattr(execute.consistency_check, "check_day",
                        lambda day: {"ok": True, "conflicts": [], "warnings": []})
    execution_cache(team, DAY, {code: {"open": 53, "high": 54, "low": 52,
                                     "close": 53.5, "prev_close": 54}
                              for code in ("601318", "000300")})
    return reviews


def plan_hash(fixture):
    return json.loads((fixture[1].team / "decisions" / DAY / "decision.json").read_text())["plan_hash"]


def accounts(team):
    return {name: path.read_bytes() for name, path in paper_pipeline.paper_db_paths(team).items() if path.exists()}


@pytest.mark.parametrize("decision", ["approved", "reduced"])
def test_new_positive_approval_requires_all_current_reviews(workspace, decision):
    team = workspace[1].team
    path = team / "decisions" / DAY / "decision.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Multica review"):
        approvals.record(DAY, "601318", "fixture", decision, plan_hash(workspace))
    assert path.read_bytes() == before
    assert not (team / "loops/approvals.json").exists()


def test_rejection_remains_available_without_review(workspace):
    result = approvals.record(DAY, "601318", "fixture", "rejected", plan_hash(workspace))
    assert result["decision"] == "rejected"


@pytest.mark.parametrize("confirm", [False, True])
def test_public_paper_gate_blocks_before_account_creation(workspace, confirm):
    with pytest.raises(ValueError, match="Multica review"):
        paper_pipeline.run_paper_day(DAY, team=workspace[1].team, confirm=confirm)
    assert not accounts(workspace[1].team)


def test_accepted_reviews_bind_both_paper_receipts_and_retry(workspace):
    module, runner, client, _ = workspace
    accept_all(workspace)
    calls = dict(client.calls)
    proof = module.require_reviews(DAY, plan_hash(workspace), team=runner.team)
    result = paper_pipeline.run_paper_day(DAY, team=runner.team)
    assert result["gate"]["fills"] == result["no_gate"]["fills"] == 1
    assert len(proof["receipts"]) == 6 and proof["gate_hash"]
    for path in paper_pipeline.paper_db_paths(runner.team).values():
        with sqlite3.connect(path) as conn:
            receipt = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
        assert receipt["execution_inputs"]["review_gate"] == proof
    before = accounts(runner.team)
    assert paper_pipeline.run_paper_day(DAY, team=runner.team)["gate"]["status"] == "skipped"
    assert accounts(runner.team) == before and dict(client.calls) == calls


def test_active_cohort_publication_reviews_and_paper_are_one_version(meeting, execution_cache):
    from trading_team import multica_reviews
    runner, client, _ = meeting
    team = runner.team
    config = team.parent / "paper/active_cohort.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "schema": "paper_cohort.v1", "cohort": f"{DAY}-forward",
        "start_date": DAY, "initial_cash": 100000,
        "accounts": {name: f"paper/cohort_{DAY}/llm_{name}.db" for name in ("gate", "no_gate")},
    }))
    runner.run(DAY, collect=False, push=False, publish_only=True)
    state = {"project_id": str(uuid4()), "squad_id": str(uuid4()), "workspace_id": str(uuid4()),
             "agent_ids": {name: str(uuid4()) for name in
                           ["Research Lead", *[role["name"] for role in multica_reviews.ROLES.values()]]}}
    path = team / ".runs/multica/bootstrap.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state))
    execution_cache(team, DAY, {code: {"open": 53, "high": 54, "low": 52,
                                      "close": 53.5, "prev_close": 54}
                               for code in ("601318", "000300")})
    fixture = (multica_reviews, runner, client, state)
    accept_all(fixture)
    result = daily_finalize.run_finalize_workflow(DAY, team=team, paper=True, replay=False)
    assert result["status"] == "succeeded"
    paths = paper_pipeline.paper_db_paths(team)
    assert all(path.parent.name == f"cohort_{DAY}" for path in paths.values())
    assert result["paper"]["gate"]["fills"] == result["paper"]["no_gate"]["fills"] == 1
    before = accounts(team)
    assert daily_finalize.run_finalize_workflow(DAY, team=team, paper=True, replay=False)["status"] == "reused"
    assert accounts(team) == before
    assert not (team.parent / "paper/llm_gate.db").exists()
    assert not (team.parent / "paper/llm_no_gate.db").exists()


@pytest.mark.parametrize("damage", ["new_reject", "plan_note", "report", "bundle", "failed"])
def test_current_review_gate_not_bypassed_by_existing_approval(workspace, damage):
    module, runner, _, _ = workspace
    accept_all(workspace)
    approvals.record(DAY, "601318", "fixture", "approved", plan_hash(workspace), request_id="original")
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    if damage == "new_reject":
        submit(workspace, bundle, response(module, bundle, "reject"))
    elif damage == "plan_note":
        path = runner.team / "outputs" / DAY / "plan.json"
        doc = json.loads(path.read_text()); doc["note"] = "changed non-trade evidence"
        path.write_text(json.dumps(doc))
    elif damage == "report":
        (runner.team / "outputs" / DAY / "01_fundamental.md").write_text("changed")
    elif damage == "bundle":
        module.bundle_path(runner.team, DAY, bundle["bundle_hash"]).unlink()
    else:
        run = runner.ledger.start_run("multica_review.data-quality", DAY, trigger_source="fixture", resume=False)
        runner.ledger.finish_run(run, status="failed", error="fixture interruption")
    with pytest.raises(ValueError, match="Multica review"):
        paper_pipeline.run_paper_day(DAY, team=runner.team)
    assert execute.execute(DAY, confirm=True) is False
    assert not accounts(runner.team) and not execute.DB.exists()
    with pytest.raises(ValueError, match="Multica review"):
        approvals.record(DAY, "601318", "fixture", "approved", plan_hash(workspace), request_id="new")


def test_gate_binds_requested_plan_not_another_hash(workspace):
    accept_all(workspace)
    with pytest.raises(ValueError, match="Multica review"):
        workspace[0].require_reviews(DAY, "0" * 64, team=workspace[1].team)


def test_notification_requires_current_independent_reviews(workspace, monkeypatch):
    team = workspace[1].team
    monkeypatch.setattr(push_feishu, "_config", lambda: pytest.fail("notification config read before review gate"))
    with pytest.raises(ValueError, match="Multica review"):
        push_feishu.push(DAY, team=team)
    assert not (team / ".runs/outbox").exists()

    accept_all(workspace)
    with pytest.raises(ValueError, match="Paper evidence missing"):
        push_feishu.push(DAY, team=team)
    assert not (team / ".runs/outbox").exists()
    paper_pipeline.run_paper_day(DAY, team=team)
    assert push_feishu._verify_source(DAY, team) == plan_hash(workspace)


def test_notification_rejects_one_changed_paper_receipt_before_outbox(workspace, monkeypatch):
    team = workspace[1].team
    accept_all(workspace)
    paper_pipeline.run_paper_day(DAY, team=team)
    path = paper_pipeline.paper_db_paths(team)["no_gate"]
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE execution_receipts SET plan_hash=?", ("0" * 64,))
    monkeypatch.setattr(push_feishu, "_config", lambda: pytest.fail("notification credentials read"))
    with pytest.raises(ValueError, match="Paper evidence missing"):
        push_feishu.push(DAY, team=team)
    assert not (team / ".runs/outbox").exists()


def test_notification_card_uses_verified_daily_inputs_without_index(workspace):
    team = workspace[1].team
    assert not (team / "outputs/index.json").exists()
    plan = json.loads((team / "outputs" / DAY / "plan.json").read_text())
    manifest = json.loads((team / "context" / DAY / "manifest.json").read_text())

    card = push_feishu.build_card(DAY, team)

    detail = next(item["text"]["content"] for item in card["elements"]
                  if item.get("text", {}).get("tag") == "plain_text")
    assert f"数据截至 {plan['data_asof']}" in detail
    assert ("采集正常" if manifest["ok"] else "采集部分源异常") in detail
    (team / "outputs/index.json").write_text(json.dumps([
        {"date": DAY, "data_asof": "2000-01-01", "collect_ok": False, "ratings_line": "stale"},
    ]))
    assert push_feishu.build_card(DAY, team) == card


def test_notification_card_does_not_call_rejected_plans_approved(workspace):
    team = workspace[1].team
    path = team / "decisions" / DAY / "decision.json"
    decision = json.loads(path.read_text())
    for plan in decision["plans"]:
        plan["approval"] = "rejected"
    path.write_text(json.dumps(decision))

    card = push_feishu.build_card(DAY, team)
    contents = [item.get("text", {}).get("content", "") for item in card["elements"]]
    assert not any("已全部批复" in content for content in contents)
    assert any(f"否决 {len(decision['plans'])}" in content for content in contents)
    assert any("批准 0" in content for content in contents)


def test_finalize_recovers_at_review_barrier_without_new_research(workspace):
    module, runner, client, _ = workspace
    with pytest.raises(ValueError, match="Multica review"):
        daily_finalize.run_finalize_workflow(DAY, team=runner.team, paper=True, replay=False)
    failed = runner.ledger.latest_run("daily_finalize", DAY)
    assert runner.ledger.latest_step(failed["run_id"], "review_gate")["status"] == "failed"
    assert not accounts(runner.team)
    accept_all(workspace)
    calls = dict(client.calls)
    result = daily_finalize.run_finalize_workflow(DAY, team=runner.team, paper=True, replay=False)
    assert result["status"] == "succeeded" and result["paper"]["gate"]["fills"] == 1
    assert len(result["review_gate"]["receipts"]) == 6
    assert dict(client.calls) == calls
    assert daily_finalize.run_finalize_workflow(DAY, team=runner.team, paper=True, replay=False)["status"] == "reused"
    before = accounts(runner.team)
    bundle = module.prepare_review(DAY, "data-quality", team=runner.team)
    submit(workspace, bundle, response(module, bundle, "reject"))
    with pytest.raises(ValueError, match="Multica review"):
        daily_finalize.run_finalize_workflow(DAY, team=runner.team, paper=True, replay=False)
    assert accounts(runner.team) == before


def test_rejected_finalize_stops_before_decision_materialization(workspace):
    _, runner, _, _ = workspace
    team = runner.team
    decision_dir = team / "decisions" / DAY
    before = {path.name: path.read_bytes() for path in decision_dir.iterdir() if path.is_file()}

    with pytest.raises(ValueError, match="Multica review"):
        daily_finalize.run_finalize_workflow(DAY, team=team, paper=True, replay=False)

    run = runner.ledger.latest_run("daily_finalize", DAY)
    assert [step["step_name"] for step in runner.ledger.get_run(run["run_id"])["steps"]] == [
        "session_gate", "review_gate",
    ]
    assert {path.name: path.read_bytes() for path in decision_dir.iterdir() if path.is_file()} == before
    assert not accounts(team)


def test_rejected_finalize_does_not_touch_plan_backfill(workspace, monkeypatch):
    team = workspace[1].team
    path = team / "outputs" / DAY / "plan.json"
    before = path.read_bytes()
    original = daily_finalize.ensure_paper_proposals
    calls = []

    def backfill(day, root):
        calls.append(day)
        return original(day, root)

    monkeypatch.setattr(daily_finalize, "ensure_paper_proposals", backfill)

    with pytest.raises(ValueError, match="Multica review"):
        daily_finalize.run_finalize_workflow(DAY, team=team, paper=True, replay=False)

    assert calls == []
    assert path.read_bytes() == before
    assert not accounts(team)


def test_paper_finalize_requires_complete_immutable_plan(workspace):
    team = workspace[1].team
    path = team / "outputs" / DAY / "plan.json"
    plan = json.loads(path.read_text())
    plan.pop("paper_proposals", None)
    path.write_text(json.dumps(plan))
    before = path.read_bytes()

    with pytest.raises(ValueError, match="Paper plan proposals missing"):
        daily_finalize.run_finalize_workflow(DAY, team=team, paper=True, replay=False)

    assert path.read_bytes() == before
    assert not accounts(team)


def test_non_executing_materialization_does_not_require_reviews(workspace):
    result = daily_finalize.run_finalize_workflow(DAY, team=workspace[1].team, paper=False, replay=False)
    assert result["status"] == "succeeded" and not accounts(workspace[1].team)


def test_blocked_paper_keeps_non_executing_counterfactual(workspace):
    team = workspace[1].team
    with pytest.raises(ValueError, match="Multica review"):
        daily_finalize.run_finalize_workflow(DAY, team=team, paper=True, replay=True)
    assert (team / "outputs" / DAY / "counterfactual.json").is_file()
    counterfactual = json.loads((team / "outputs" / DAY / "counterfactual.json").read_text())
    assert counterfactual["review_gate"]["passed"] is False
    assert counterfactual["review_gate"]["status"] == "incomplete"
    assert "独立审阅未通过" in (team / "outputs" / DAY / "counterfactual.md").read_text()
    assert (team / "outputs" / DAY / "continuous_counterfactual.json").is_file()
    run = workspace[1].ledger.latest_run("daily_finalize", DAY)
    assert [step["step_name"] for step in workspace[1].ledger.get_run(run["run_id"])["steps"]] == [
        "session_gate", "counterfactual", "review_gate",
    ]
    assert not any(step["step_name"] == "materialize" for step in workspace[1].ledger.get_run(run["run_id"])["steps"])
    assert not accounts(team)


def test_new_review_version_cannot_recertify_settled_receipt(workspace):
    accept_all(workspace)
    paper_pipeline.run_paper_day(DAY, team=workspace[1].team)
    before = accounts(workspace[1].team)
    accept_all(workspace)
    with pytest.raises(ValueError, match="execution evidence changed"):
        paper_pipeline.run_paper_day(DAY, team=workspace[1].team)
    assert accounts(workspace[1].team) == before


def test_finalize_cache_cannot_pair_new_reviews_with_old_settlement(workspace):
    accept_all(workspace)
    team = workspace[1].team
    daily_finalize.run_finalize_workflow(DAY, team=team, paper=True, replay=False)
    before = accounts(team)
    accept_all(workspace)
    with pytest.raises(ValueError, match="Paper evidence changed"):
        daily_finalize.run_finalize_workflow(DAY, team=team, paper=True, replay=False)
    assert accounts(team) == before


def test_readonly_review_api_uses_current_workspace_and_requires_date(workspace, monkeypatch):
    spec = importlib.util.spec_from_file_location("review_console_test", Path(__file__).resolve().parents[1] / "console/serve.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", workspace[1].team.parent)
    handler = object.__new__(module.H)
    result = []
    handler._send_json = lambda status, payload: result.append((status, payload))
    handler.send_error = lambda *args: pytest.fail("review API is missing")
    handler.path = "/api/multica-reviews?date=" + DAY
    handler.do_GET()
    assert result[-1][0] == 200 and result[-1][1]["review"]["status"] == "incomplete"
    handler.path = "/api/multica-reviews?date=../bad"
    handler.do_GET()
    assert result[-1][0] == 400


def test_api_blocked_approval_is_explicit_and_not_recorded(workspace):
    spec = importlib.util.spec_from_file_location("review_approval_console_test", Path(__file__).resolve().parents[1] / "console/serve.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    handler = object.__new__(module.H)
    body = json.dumps({"date": DAY, "code": "601318", "decision": "approved",
                       "plan_hash": plan_hash(workspace), "request_id": "blocked-request"}).encode()
    handler.path = "/api/approve"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    sent = []
    handler._send_json = lambda status, payload: sent.append((status, payload))
    handler.do_POST()
    assert sent[0][0] == 409 and sent[0][1]["code"] == "review_gate_blocked"
    assert not (workspace[1].team / "loops/approvals.json").exists()


def test_reviewed_settlement_restores_with_same_gate_and_no_duplicate(workspace, tmp_path):
    from trading_team.workflow_recovery import backup_workflow, restore_workflow
    accept_all(workspace)
    team = workspace[1].team
    paper_pipeline.run_paper_day(DAY, team=team)
    backup = backup_workflow(team=team, target=tmp_path.parent / (tmp_path.name + "-backup"))
    target = tmp_path.parent / (tmp_path.name + "-restore")
    restore_workflow(Path(backup["path"]), backup["manifest_hash"], target)
    restored = target / "trading_team"
    before = accounts(restored)
    assert workspace[0].require_reviews(DAY, plan_hash(workspace), team=restored) == workspace[0].require_reviews(DAY, plan_hash(workspace), team=team)
    result = paper_pipeline.run_paper_day(DAY, team=restored)
    assert result["gate"]["status"] == result["no_gate"]["status"] == "skipped"
    assert accounts(restored) == before
