import fcntl
import gc
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from trading_team.run_ledger import RunLedger

SHANGHAI = ZoneInfo("Asia/Shanghai")
START = datetime(2026, 9, 21, 16, tzinfo=SHANGHAI)


@pytest.fixture
def workspace(tmp_path, execution_cache, monkeypatch):
    team = tmp_path / "trading_team"
    team.mkdir()
    calendar = tmp_path / "calendar.npy"
    np.save(calendar, np.array([20260918, 20260921, 20260922, 20260923, 20260924, 20260928]))
    (team / "watchlist.json").write_text('{"stocks":[{"code":"600036"}]}')
    bar = {"open": 10, "high": 10.2, "low": 9.8, "close": 10.1, "prev_close": 10}
    for day in ("2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"):
        execution_cache(team, day, {code: bar for code in ("600036", "000300")})
    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: pytest.fail("network called"))
    return team, calendar


def tick(workspace, now=START):
    from trading_team.archive_service import tick
    return tick(team=workspace[0], calendar_path=workspace[1], now=now, trigger_source="test")


def status(workspace, now=START):
    from trading_team.archive_service import service_status
    return service_status(team=workspace[0], calendar_path=workspace[1], now=now)


def test_tick_captures_once_then_revalidates_without_new_run(workspace):
    team, _ = workspace
    first = tick(workspace)
    ledger = RunLedger(team / ".runs/ledger.db", readonly=True)
    runs = ledger.list_runs()
    assert first["status"] == "healthy"
    assert first["expected_session"] == "2026-09-21"
    assert first["captured"] == ["2026-09-21"]
    assert runs[0]["trigger_source"] == "test"
    second = tick(workspace, START + timedelta(minutes=5))
    assert second["status"] == "healthy" and second["captured"] == []
    assert ledger.list_runs() == runs
    assert not (team.parent / "paper").exists()


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 9, 22, 15, 9), "2026-09-21"),
    (datetime(2026, 9, 22, 15, 10), "2026-09-22"),
    (datetime(2026, 9, 22, 7, 10, tzinfo=ZoneInfo("UTC")), "2026-09-22"),
    (datetime(2026, 9, 26, 16), "2026-09-24"),
])
def test_tick_obeys_shanghai_completed_session_and_holiday(workspace, now, expected):
    result = tick(workspace, now)
    assert result["expected_session"] == expected
    assert result["captured"] == [expected]


def test_missed_days_remain_visible_but_do_not_block_current_capture(workspace, monkeypatch):
    from trading_team import archive_service
    tick(workspace)
    capture = archive_service.capture_closing
    calls = []

    def missing_day(day, **kwargs):
        calls.append(day)
        if day == "2026-09-22":
            raise ValueError("exact historical closing quote unavailable")
        return capture(day, **kwargs)

    monkeypatch.setattr(archive_service, "capture_closing", missing_day)
    now = START + timedelta(days=2)
    result = tick(workspace, now)
    assert result["status"] == "degraded"
    assert calls == ["2026-09-23", "2026-09-22"]
    assert result["missing_sessions"] == ["2026-09-22"]
    assert result["latest_verified_session"] == "2026-09-23"
    assert status(workspace, now)["missing_sessions"] == ["2026-09-22"]


def test_retry_backoff_preserves_failure_until_success(workspace, monkeypatch):
    from trading_team import archive_service
    original = archive_service.capture_closing
    calls = []

    def fail(day, **kwargs):
        calls.append(day)
        raise ValueError("temporary source failure")

    monkeypatch.setattr(archive_service, "capture_closing", fail)
    first = tick(workspace)
    assert first["status"] == "degraded"
    assert first["errors"]["2026-09-21"]["attempts"] == 1
    second = tick(workspace, START + timedelta(minutes=1))
    assert len(calls) == 1
    assert second["errors"] == first["errors"]
    third = tick(workspace, START + timedelta(minutes=5))
    assert len(calls) == 2
    assert third["errors"]["2026-09-21"]["attempts"] == 2
    monkeypatch.setattr(archive_service, "capture_closing", original)
    result = tick(workspace, START + timedelta(minutes=15))
    assert result["status"] == "healthy" and not result["errors"]


def test_archive_tampering_is_visible_without_repair_or_ledger_write(workspace):
    team, _ = workspace
    tick(workspace)
    state = json.loads((team / ".runs/archive-service.json").read_bytes())
    receipt = state["receipts"]["2026-09-21"]
    path = team / "market_archive/2026-09-21" / f"{receipt['payload_hash']}.json"
    path.write_text("bad")
    # Finish pending SQLite connection finalizers before measuring the read-only observer.
    gc.collect()
    ledger_before = (team / ".runs/ledger.db").read_bytes()
    result = status(workspace)
    assert result["status"] == "degraded"
    assert result["invalid_sessions"] == ["2026-09-21"]
    assert result["latest_verified_session"] is None
    assert path.read_text() == "bad"
    assert (team / ".runs/ledger.db").read_bytes() == ledger_before


def test_stale_heartbeat_and_newly_due_session_are_not_healthy(workspace):
    tick(workspace)
    stale = status(workspace, START + timedelta(minutes=11))
    assert stale["heartbeat"] == "stale" and stale["status"] == "degraded"
    next_day = status(workspace, START + timedelta(days=1))
    assert next_day["missing_sessions"] == ["2026-09-22"]


def test_missing_state_read_does_not_create_state(workspace):
    team, _ = workspace
    result = status(workspace)
    assert result["status"] == "uninitialized"
    assert not (team / ".runs").exists()


def test_corrupt_state_is_not_reset_and_treated_as_new_installation(workspace):
    team, _ = workspace
    (team / ".runs").mkdir()
    path = team / ".runs/archive-service.json"
    path.write_text("broken")
    assert status(workspace)["status"] == "invalid"
    with pytest.raises(ValueError, match="state"):
        tick(workspace)
    assert path.read_text() == "broken"


def test_damaged_calendar_is_reported_without_capturing(workspace, monkeypatch):
    from trading_team import archive_service
    team, calendar = workspace
    calendar.write_bytes(b"broken")
    monkeypatch.setattr(archive_service, "capture_closing", lambda *_a, **_k: pytest.fail("capture called"))
    result = tick(workspace)
    assert result["status"] == "failed"
    assert "calendar" in result["error"]
    assert status(workspace)["status"] == "failed"


def test_calendar_recovery_does_not_forget_days_since_first_check(workspace):
    calendar = workspace[1]
    original = calendar.read_bytes()
    calendar.write_bytes(b"bad")
    assert tick(workspace)["status"] == "failed"
    calendar.write_bytes(original)
    result = tick(workspace, START + timedelta(days=2))
    assert result["tracking_start"] == "2026-09-21"
    assert result["captured"] == ["2026-09-23", "2026-09-22", "2026-09-21"]


def test_service_lock_does_not_overwrite_an_active_tick(workspace):
    team, _ = workspace
    path = team / ".runs/locks/archive-service.lock"
    path.parent.mkdir(parents=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            tick(workspace)
    assert not (team / ".runs/archive-service.json").exists()


def test_new_watchlist_symbol_invalidates_current_receipt_coverage(workspace, execution_cache):
    team, _ = workspace
    tick(workspace)
    (team / "watchlist.json").write_text('{"stocks":[{"code":"600036"},{"code":"601318"}]}')
    assert status(workspace)["invalid_sessions"] == ["2026-09-21"]
    bar = {"open": 10, "high": 10.2, "low": 9.8, "close": 10.1, "prev_close": 10}
    execution_cache(team, "2026-09-21", {code: bar for code in ("600036", "601318", "000300")})
    assert tick(workspace, START + timedelta(minutes=5))["status"] == "healthy"


def test_failed_tick_cli_returns_nonzero(monkeypatch, capsys):
    from trading_team import archive_service
    monkeypatch.setattr("sys.argv", ["archive_service", "tick"])
    monkeypatch.setattr(archive_service, "tick", lambda **_kwargs: {"status": "degraded"})
    with pytest.raises(SystemExit) as error:
        archive_service.main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"


def test_backlog_work_is_bounded_without_discarding_old_gaps(workspace):
    tick(workspace, datetime(2026, 9, 21, 8, tzinfo=SHANGHAI))
    now = datetime(2026, 9, 24, 16, tzinfo=SHANGHAI)
    first = tick(workspace, now)
    assert first["captured"] == ["2026-09-24", "2026-09-23", "2026-09-22"]
    assert first["missing_sessions"] == ["2026-09-21"]
    assert first["status"] == "degraded"
    second = tick(workspace, now + timedelta(minutes=5))
    assert second["captured"] == ["2026-09-21"]
    assert second["verified_count"] == 5 and second["status"] == "healthy"


def test_interrupted_tick_cannot_look_healthy_but_can_resume(workspace):
    team, _ = workspace
    tick(workspace)
    path = team / ".runs/archive-service.json"
    state = json.loads(path.read_bytes())
    state["status"] = "running"
    path.write_text(json.dumps(state))
    assert status(workspace)["status"] == "degraded"
    result = tick(workspace, START + timedelta(minutes=5))
    assert result["status"] == "healthy" and result["captured"] == []


def test_tick_does_not_mutate_existing_paper_database(workspace):
    team, _ = workspace
    paper = team.parent / "paper"
    paper.mkdir()
    path = paper / "llm_no_gate.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE positions(code TEXT, qty INTEGER)")
    conn.execute("INSERT INTO positions VALUES('600036',100)")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    assert tick(workspace)["status"] == "healthy"
    assert status(workspace)["status"] == "healthy"
    assert path.read_bytes() == before
    assert not (paper / "llm_gate.db").exists()
