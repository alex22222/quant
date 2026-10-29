import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from trading_team.run_ledger import RunLedger


def test_workflow_attempts_keep_sources_context_and_terminal_errors(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    first_context = {"provider": "multica", "task_id": "task-one"}
    run_id = ledger.start_run("daily_research", "2026-09-22", trigger_source="multica:issue",
                              external_context=first_context)
    first_step = ledger.start_step(run_id, "role_01")
    ledger.finish_step(first_step, status="failed", error="timeout")
    ledger.finish_run(run_id, status="failed", error="timeout", metadata={"result": "failed"})
    resumed = ledger.start_run("daily_research", "2026-09-22", trigger_source="manual",
                               external_context={})
    second_step = ledger.start_step(run_id, "role_01")
    ledger.finish_step(second_step)
    ledger.finish_run(run_id, status="succeeded", metadata={"result": "complete"})
    run = ledger.get_run(run_id)
    assert resumed == run_id
    assert [(a["attempt"], a["trigger_source"], a["status"]) for a in run["attempts"]] == [
        (1, "multica:issue", "failed"), (2, "manual", "succeeded"),
    ]
    assert run["attempts"][0]["external_context"] == first_context
    assert run["attempts"][0]["error"] == "timeout"
    assert [s["workflow_attempt"] for s in run["steps"]] == [1, 2]
    assert run["attempts"][1]["external_context"] == {}


def test_resumed_open_attempt_is_superseded_without_faking_completion(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    run_id = ledger.start_run("daily_finalize", "2026-09-22", trigger_source="first")
    ledger.start_run("daily_finalize", "2026-09-22", trigger_source="retry")
    attempts = ledger.get_run(run_id)["attempts"]
    assert attempts[0]["status"] == "superseded"
    assert attempts[1]["status"] == "running"



def test_legacy_migration_preserves_history_without_inventing_attempts(tmp_path):
    path = tmp_path / "runs.db"
    ledger = RunLedger(path)
    run_id = ledger.start_run("daily_finalize", "2026-09-22", trigger_source="legacy")
    step = ledger.start_step(run_id, "paper")
    ledger.finish_step(step)
    ledger.finish_run(run_id, status="succeeded")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE workflow_attempts")
        conn.execute("ALTER TABLE step_runs DROP COLUMN workflow_attempt")
    barrier = Barrier(2)

    def migrate():
        barrier.wait()
        return RunLedger(path).get_run(run_id)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: migrate(), range(2)))
    for result in results:
        assert result["status"] == "succeeded"
        assert result["attempts"] == []
        assert result["steps"][0]["workflow_attempt"] is None


def test_run_ledger_records_retryable_workflow(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    run_id = ledger.start_run(
        "daily_research",
        "2026-09-21",
        trigger_source="manual",
        model="kimi-k3",
        input_hash="input-v1",
    )
    step_id = ledger.start_step(
        run_id,
        "role_01",
        role="fundamental",
        input_hash="bundle-v1",
        prompt_hash="prompt-v1",
        model="kimi-k3",
    )
    ledger.finish_step(step_id, status="failed", error="timeout")
    ledger.finish_run(run_id, status="failed", error="role_01: timeout")

    resumed_id = ledger.start_run(
        "daily_research",
        "2026-09-21",
        trigger_source="retry",
        model="kimi-k3",
        input_hash="input-v1",
    )
    assert resumed_id == run_id

    retry_step = ledger.start_step(run_id, "role_01", role="fundamental")
    ledger.finish_step(
        retry_step,
        output_hash="output-v1",
        artifact_path="outputs/2026-09-21/01_fundamental.md",
    )
    ledger.finish_run(run_id, status="succeeded", data_asof="2026-09-18")

    run = ledger.get_run(run_id)
    assert run["status"] == "succeeded"
    assert run["attempt"] == 2
    assert [step["attempt"] for step in run["steps"]] == [1, 2]
    assert run["steps"][0]["error"] == "timeout"
    assert run["steps"][1]["output_hash"] == "output-v1"


def test_run_ledger_creates_new_run_after_success(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    first = ledger.start_run("daily_research", "2026-09-21", trigger_source="manual")
    ledger.finish_run(first, status="succeeded")
    second = ledger.start_run("daily_research", "2026-09-21", trigger_source="manual")

    assert second != first
    assert len(ledger.list_runs()) == 2


def test_changed_input_does_not_resume_failed_run(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    first = ledger.start_run(
        "daily_finalize", "2026-09-21", trigger_source="manual", input_hash="plan-v1"
    )
    ledger.finish_run(first, status="failed", error="paper failed")
    second = ledger.start_run(
        "daily_finalize", "2026-09-21", trigger_source="retry", input_hash="plan-v2"
    )

    assert second != first


def test_success_and_latest_step_can_be_looked_up_by_version(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    run_id = ledger.start_run(
        "daily_finalize", "2026-09-21", trigger_source="manual", input_hash="plan-v1"
    )
    step_id = ledger.start_step(run_id, "materialize")
    ledger.finish_step(step_id, metadata={"result": {"plan_hash": "plan-v1"}})
    ledger.finish_run(run_id, status="succeeded")

    assert ledger.find_successful_run("daily_finalize", "2026-09-21", "plan-v1")
    assert ledger.find_successful_run("daily_finalize", "2026-09-21", "plan-v2") is None
    assert ledger.latest_step(run_id, "materialize")["metadata"]["result"]["plan_hash"] == "plan-v1"
    assert "metadata" not in ledger.list_runs()[0]
