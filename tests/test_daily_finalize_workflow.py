import json

import pytest

from trading_team.daily_finalize import materialize_day, run_finalize_workflow
from trading_team.run_ledger import RunLedger

pytestmark = pytest.mark.usefixtures("finalize_calendar")


def _write_plan(team):
    output = team / "outputs" / "2026-09-21"
    output.mkdir(parents=True)
    (output / "plan.json").write_text(json.dumps({
        "date": "2026-09-21",
        "data_asof": "2026-09-18",
        "market_ok": False,
        "proposals": [],
        "paper_proposals": [],
    }), encoding="utf-8")


def test_finalize_resumes_only_failed_step(tmp_path, monkeypatch, stub_research_reviews):
    team = tmp_path / "trading_team"
    _write_plan(team)
    ledger = RunLedger(tmp_path / "ledger.db")
    calls = {"materialize": 0, "counterfactual": 0, "paper": 0}

    def materialize(day, root):
        calls["materialize"] += 1
        return materialize_day(day, root)

    def counterfactual(day, root):
        calls["counterfactual"] += 1
        output = root / "outputs" / day / "counterfactual.json"
        output.write_text('{"ok":true}', encoding="utf-8")
        output.with_suffix(".md").write_text("verified replay", encoding="utf-8")
        cache = root / "replay_data" / f"{day}.json"
        cache.parent.mkdir(exist_ok=True)
        cache.write_text('{"bars":{}}', encoding="utf-8")
        return {"ok": True}

    def paper(day, team):
        calls["paper"] += 1
        if calls["paper"] == 1:
            raise RuntimeError("temporary paper failure")
        return {"status": "skipped", "reason": "same-day retrospective fixture"}

    monkeypatch.setattr("trading_team.daily_finalize.materialize_day", materialize)
    monkeypatch.setattr("trading_team.daily_finalize.write_counterfactual", counterfactual)
    monkeypatch.setattr("trading_team.daily_finalize.run_paper_day", paper)

    with pytest.raises(RuntimeError, match="temporary paper failure"):
        run_finalize_workflow(
            "2026-09-21", team=team, replay=True, paper=True, ledger=ledger
        )
    result = run_finalize_workflow(
        "2026-09-21", team=team, replay=True, paper=True, ledger=ledger
    )

    assert result["status"] == "succeeded"
    assert calls == {"materialize": 1, "counterfactual": 1, "paper": 2}
    run = ledger.get_run(result["run_id"])
    assert [step["step_name"] for step in run["steps"]] == [
        "session_gate", "counterfactual", "review_gate", "materialize", "paper", "paper",
    ]


def test_finalize_reuses_successful_plan_version(tmp_path):
    team = tmp_path / "trading_team"
    _write_plan(team)
    ledger = RunLedger(tmp_path / "ledger.db")

    first = run_finalize_workflow(
        "2026-09-21", team=team, replay=False, paper=False, ledger=ledger
    )
    second = run_finalize_workflow(
        "2026-09-21", team=team, replay=False, paper=False, ledger=ledger
    )

    assert first["status"] == "succeeded"
    assert second == {
        "date": "2026-09-21",
        "status": "reused",
        "run_id": first["run_id"],
        "plan_hash": first["plan_hash"],
    }


def test_cli_focus_date_does_not_request_full_history(monkeypatch):
    from trading_team import daily_finalize
    calls = []
    monkeypatch.setattr("sys.argv", ["daily_finalize", "--focus-date", "2026-09-21"])
    monkeypatch.setattr(daily_finalize, "finalize_available_tracked",
                        lambda **kwargs: calls.append(kwargs) or [])
    daily_finalize.main()
    assert calls == [{"focus_day": "2026-09-21"}]


def test_cli_rejects_ambiguous_dates(monkeypatch):
    from trading_team import daily_finalize
    monkeypatch.setattr("sys.argv", ["daily_finalize", "2026-09-18", "--focus-date", "2026-09-21"])
    with pytest.raises(SystemExit) as error:
        daily_finalize.main()
    assert error.value.code == 2
