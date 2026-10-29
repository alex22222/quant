import json
import fcntl
import sqlite3
from datetime import datetime

import pytest

from trading_team import execution_bars


DAY = "2026-09-21"
NOW = datetime(2026, 9, 22, 8)


@pytest.fixture
def archived_team(tmp_path, execution_cache, finalize_calendar, monkeypatch):
    team = tmp_path / "original" / "trading_team"
    bar = {"open": 10, "high": 10.2, "low": 9.8, "close": 10.1, "prev_close": 10}
    execution_cache(team, DAY, {code: bar for code in ("600036", "000300")})
    (team / "watchlist.json").write_text(json.dumps({"stocks": [{"code": "600036"}]}))
    monkeypatch.setattr(execution_bars, "fetch_snapshot_tx", lambda *_: pytest.fail("network called"))
    return team, finalize_calendar


def capture(team, calendar, day="2026-09-22", now=NOW):
    from trading_team.market_archive import capture_closing
    return capture_closing(day, team=team, calendar_path=calendar, now=now)


def test_loader_archives_existing_cache_without_rewriting_projection(archived_team):
    team, calendar = archived_team
    path = team / "replay_data" / f"{DAY}.json"
    before = path.read_bytes()
    execution_bars.load_execution_bars(DAY, ["600036"], team, calendar_path=calendar, now=NOW)
    payload = json.loads(before)
    archived = team / "market_archive" / DAY / f"{payload['payload_hash']}.json"
    assert json.loads(archived.read_bytes()) == payload
    assert path.read_bytes() == before


def test_capture_and_restore_are_offline_and_preserve_replay_and_valuation(archived_team, tmp_path):
    from trading_team.market_archive import restore_archive, verify_receipt

    team, calendar = archived_team
    receipt = capture(team, calendar)
    assert receipt == capture(team, calendar)
    assert verify_receipt(team, receipt)["payload_hash"] == receipt["payload_hash"]
    target = tmp_path / "recovered"
    restored = restore_archive(team, receipt["receipt_hash"], target)
    assert restored["status"] == "verified"
    restored_calendar = target / "market_archive/calendars" / f"{receipt['calendar_hash']}.npy"
    assert execution_bars._cached_bars(DAY, target) == execution_bars._cached_bars(DAY, team)
    assert execution_bars.load_closing_marks(target, now=NOW, calendar_path=restored_calendar) == (
        execution_bars.load_closing_marks(team, now=NOW, calendar_path=calendar))
    assert not list(target.rglob("*.db"))
    assert verify_receipt(target, receipt) == verify_receipt(team, receipt)


@pytest.mark.parametrize("part", ["data", "calendar", "receipt"])
def test_tampered_archive_cannot_be_restored(archived_team, tmp_path, part):
    from trading_team.market_archive import restore_archive

    team, calendar = archived_team
    receipt = capture(team, calendar)
    paths = {
        "data": team / "market_archive" / DAY / f"{receipt['payload_hash']}.json",
        "calendar": team / "market_archive/calendars" / f"{receipt['calendar_hash']}.npy",
        "receipt": team / "market_archive/receipts" / f"{receipt['receipt_hash']}.json",
    }
    paths[part].write_bytes(b"corrupt")
    target = tmp_path / "recovered"
    with pytest.raises(ValueError, match="archive|hash|calendar"):
        restore_archive(team, receipt["receipt_hash"], target)
    assert not target.exists()


def test_damaged_existing_archive_is_not_silently_repaired(archived_team):
    team, calendar = archived_team
    receipt = capture(team, calendar)
    path = team / "market_archive" / DAY / f"{receipt['payload_hash']}.json"
    path.write_text("broken")
    with pytest.raises(ValueError, match="archive"):
        capture(team, calendar)
    assert path.read_text() == "broken"


def test_restoration_never_overwrites_existing_or_source_directory(archived_team, tmp_path):
    from trading_team.market_archive import restore_archive

    team, calendar = archived_team
    receipt = capture(team, calendar)
    target = tmp_path / "existing"
    target.mkdir()
    for destination in (target, team, team / "new-child"):
        with pytest.raises(ValueError, match="destination"):
            restore_archive(team, receipt["receipt_hash"], destination)
    assert not (team / "new-child").exists()


@pytest.mark.parametrize("identifier", ["../data", "a" * 63, "A" * 64, "0" * 65])
def test_restore_rejects_noncanonical_identifiers(archived_team, tmp_path, identifier):
    from trading_team.market_archive import restore_archive

    with pytest.raises(ValueError, match="hash"):
        restore_archive(archived_team[0], identifier, tmp_path / "new")


def test_capture_includes_holdings_removed_from_watchlist(archived_team, execution_cache):
    team, calendar = archived_team
    bar = {"open": 10, "high": 10.2, "low": 9.8, "close": 10.1, "prev_close": 10}
    execution_cache(team, DAY, {code: bar for code in ("600036", "601318", "000300")})
    paper = team.parent / "paper"
    paper.mkdir()
    conn = sqlite3.connect(paper / "llm_no_gate.db")
    conn.execute("CREATE TABLE positions(code TEXT, qty INTEGER)")
    conn.execute("INSERT INTO positions VALUES('601318', 100)")
    conn.commit()
    conn.close()
    receipt = capture(team, calendar)
    assert receipt["codes"] == ["000300", "600036", "601318"]


def test_capture_calendar_uses_latest_completed_session(archived_team):
    from trading_team.market_archive import completed_day

    calendar = archived_team[1]
    assert completed_day(DAY, calendar, datetime(2026, 9, 21, 15, 9)) == "2026-09-18"
    assert completed_day(DAY, calendar, datetime(2026, 9, 21, 15, 10)) == DAY
    assert completed_day("2026-09-20", calendar, NOW) == "2026-09-18"
    for business_day in ("2026-10-10", "2026-09-23"):
        with pytest.raises(ValueError, match="calendar|future"):
            completed_day(business_day, calendar, NOW)


def test_archive_keeps_old_version_when_cache_gains_symbols(archived_team, execution_cache):
    team, calendar = archived_team
    first = capture(team, calendar)
    bar = {"open": 10, "high": 10.2, "low": 9.8, "close": 10.1, "prev_close": 10}
    execution_cache(team, DAY, {code: bar for code in ("600036", "601318", "000300")})
    second = capture(team, calendar)
    assert first["payload_hash"] != second["payload_hash"]
    assert len(list((team / "market_archive" / DAY).glob("*.json"))) == 2


def test_corrupt_account_is_not_treated_as_no_holdings(archived_team):
    team, calendar = archived_team
    paper = team.parent / "paper"
    paper.mkdir()
    path = paper / "llm_gate.db"
    path.write_text("broken")
    with pytest.raises((ValueError, sqlite3.Error)):
        capture(team, calendar)
    assert path.read_text() == "broken"


def test_parallel_capture_cannot_supersede_running_attempt(archived_team):
    team, calendar = archived_team
    path = team / ".runs/locks/closing_archive-2026-09-22.lock"
    path.parent.mkdir(parents=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            capture(team, calendar)
    assert not (team / ".runs/ledger.db").exists()


def test_overview_verifies_archive_objects_without_writing(archived_team):
    from trading_team.operational_snapshot import operational_snapshot

    team, calendar = archived_team
    receipt = capture(team, calendar)
    context = team / "context/2026-09-22"
    context.mkdir(parents=True)
    (context / "execution_archive.json").write_text(json.dumps(receipt))
    (context / "manifest.json").write_text(json.dumps({
        "date": "2026-09-22", "latest_trade_date": DAY, "ok": True,
        "sources": {"execution_archive": {"ok": True}}, "execution_archive": receipt,
    }))
    assert operational_snapshot(team=team, calendar_path=calendar, now=NOW)["data"]["manifest_ok"]
    path = team / "market_archive" / DAY / f"{receipt['payload_hash']}.json"
    path.write_text("bad")
    ledger = (team / ".runs/ledger.db").read_bytes()
    assert operational_snapshot(team=team, calendar_path=calendar, now=NOW)["data"]["manifest_ok"] is False
    assert path.read_text() == "bad"
    assert (team / ".runs/ledger.db").read_bytes() == ledger
