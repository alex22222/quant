import json

import pytest

from trading_team.paper_pipeline import _db, paper_db_paths, run_paper_day
from trading_team.run_ledger import RunLedger
from trading_team.workflow_recovery import backup_workflow, verify_workflow_backup


def _cohort(team, gate="paper/cohort_2026-09-28/llm_gate.db"):
    path = team.parent / "paper/active_cohort.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "paper_cohort.v1", "cohort": "2026-09-28-forward",
        "start_date": "2026-09-28", "initial_cash": 100000,
        "accounts": {"gate": gate, "no_gate": "paper/cohort_2026-09-28/llm_no_gate.db"},
    }))
    return path


def test_active_cohort_routes_both_accounts_without_touching_legacy(tmp_path):
    team = tmp_path / "trading_team"
    team.mkdir()
    legacy = team.parent / "paper/llm_gate.db"
    legacy.parent.mkdir()
    legacy.write_bytes(b"legacy evidence")
    _cohort(team)
    paths = paper_db_paths(team)
    assert {key: path.relative_to(tmp_path).as_posix() for key, path in paths.items()} == {
        "gate": "paper/cohort_2026-09-28/llm_gate.db",
        "no_gate": "paper/cohort_2026-09-28/llm_no_gate.db",
    }
    for path in paths.values():
        with _db(path) as conn:
            assert conn.execute("SELECT day,cash,equity FROM account").fetchall() == [("init", 100000, 100000)]
            assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert legacy.read_bytes() == b"legacy evidence"


@pytest.mark.parametrize("bad", ["paper/llm_gate.db", "../escape.db", "paper/cohort_2026-09-28/llm_no_gate.db"])
def test_active_cohort_rejects_legacy_escape_or_duplicate(tmp_path, bad):
    team = tmp_path / "trading_team"
    team.mkdir()
    _cohort(team, gate=bad)
    with pytest.raises(ValueError):
        paper_db_paths(team)


def test_workflow_backup_carries_active_cohort_and_both_accounts(tmp_path):
    team = tmp_path / "source/trading_team"
    team.mkdir(parents=True)
    _cohort(team)
    paths = paper_db_paths(team)
    for path in paths.values():
        _db(path).close()
    RunLedger(team / ".runs/ledger.db")
    result = backup_workflow(team=team, target=tmp_path / "backup")
    manifest = verify_workflow_backup(tmp_path / "backup", result["manifest_hash"])
    assert "paper/active_cohort.json" in manifest["files"]
    assert {path.relative_to(team.parent).as_posix() for path in paths.values()} <= set(manifest["databases"])


def test_cohort_refuses_backdated_paper_before_any_decision(tmp_path):
    team = tmp_path / "trading_team"
    team.mkdir()
    _cohort(team)
    with pytest.raises(ValueError, match="outside active cohort"):
        run_paper_day("2026-09-24", team=team)
    assert not (team / "decisions").exists()
    assert not any((team.parent / "paper/cohort_2026-09-28").glob("*.db"))


def test_finalize_refuses_old_day_before_ledger_or_decision(tmp_path):
    from trading_team.daily_finalize import run_finalize_workflow
    team = tmp_path / "trading_team"
    team.mkdir()
    _cohort(team)
    with pytest.raises(ValueError, match="outside active cohort"):
        run_finalize_workflow("2026-09-24", team=team, paper=True)
    assert not (team / "decisions").exists()
    assert not (team / ".runs/ledger.db").exists()


def test_generation_inputs_bind_active_cohort_configuration(tmp_path):
    from trading_team.kimi_daily import ROLE_NAMES, generation_inputs
    team = tmp_path / "trading_team"
    team.mkdir()
    (team / "watchlist.json").write_text("{}")
    (team / "prompts").mkdir()
    for number, role in ROLE_NAMES.items():
        (team / "prompts" / f"{number}_{role}.md").write_text("fixture")
    config = _cohort(team)
    inputs = generation_inputs(team, "2026-09-28")
    assert "paper/active_cohort.json" in inputs
    before = inputs["paper/active_cohort.json"]
    config.write_text(config.read_text() + "\n")
    assert generation_inputs(team, "2026-09-28")["paper/active_cohort.json"] != before
