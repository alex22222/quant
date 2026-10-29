import json
from uuid import uuid4

from test_multica_reviews import DAY, meeting, submit
from trading_team import multica_reviews
from trading_team.daily_finalize import run_finalize_workflow
from trading_team.paper_pipeline import paper_db_paths


def test_publication_then_reviews_then_settlement_is_nonduplicating(meeting, execution_cache):
    runner, client, _ = meeting
    published = runner.run(DAY, collect=False, push=False, publish_only=True)
    assert published["business_complete"] is False
    index = json.loads((runner.team / "outputs/index.json").read_text(encoding="utf-8"))
    assert index[0]["date"] == DAY
    assert not (runner.team / "decisions").exists()
    state = {"project_id": str(uuid4()), "squad_id": str(uuid4()), "workspace_id": str(uuid4()),
             "agent_ids": {name: str(uuid4()) for name in
                           ["Research Lead", *[r["name"] for r in multica_reviews.ROLES.values()]]}}
    binding = runner.team / ".runs/multica/bootstrap.json"
    binding.parent.mkdir(parents=True)
    binding.write_text(json.dumps(state))
    fixture = (multica_reviews, runner, client, state)
    for role in multica_reviews.ROLES:
        submit(fixture, multica_reviews.prepare_review(DAY, role, team=runner.team))
    assert multica_reviews.review_status(DAY, team=runner.team)["status"] == "accepted"
    assert not any(path.exists() for path in paper_db_paths(runner.team).values())
    execution_cache(runner.team, DAY, {code: {"open": 53, "high": 54, "low": 52,
                                            "close": 53.5, "prev_close": 54}
                                     for code in ("601318", "000300")})
    first = run_finalize_workflow(DAY, team=runner.team, replay=False, paper=True)
    assert first["paper"]["gate"]["fills"] == first["paper"]["no_gate"]["fills"] == 1
    assert first["plan_hash"] == published["plan_hash"]
    assert run_finalize_workflow(DAY, team=runner.team, replay=False, paper=True)["status"] == "reused"
    assert sum(client.calls.values()) == 7


def test_index_failure_does_not_invalidate_verified_publication(meeting, monkeypatch):
    runner, _, _ = meeting
    monkeypatch.setattr("trading_team.build_index.build_index",
                        lambda team: (_ for _ in ()).throw(ValueError("old index input invalid")))

    published = runner.run(DAY, collect=False, push=False, publish_only=True)

    assert published["status"] == "succeeded"
    assert published["index"]["status"] == "failed"
    assert (runner.team / "outputs" / DAY / "generation.json").exists()
    run = runner.ledger.get_run(published["run_id"])
    assert run["status"] == "succeeded"
    assert runner.ledger.latest_step(run["run_id"], "build_index")["status"] == "failed"
