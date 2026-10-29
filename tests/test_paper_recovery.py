import hashlib
import json
import sqlite3

import pytest

from trading_team.paper_pipeline import _db, paper_db_paths


@pytest.fixture
def accounts(tmp_path):
    team = tmp_path / "source/trading_team"
    team.mkdir(parents=True)
    for path in paper_db_paths(team).values():
        conn = _db(path)
        conn.execute("INSERT INTO account VALUES ('2026-09-21',90000,100000,'legacy fixture')")
        conn.execute("INSERT INTO positions VALUES ('600036',200,40,'2026-09-18')")
        conn.execute("DROP TABLE execution_receipts")
        conn.commit()
        conn.close()
    return team


def backup(team, target):
    from trading_team.paper_recovery import backup_accounts
    return backup_accounts(team=team, target=target)


def test_roundtrip_preserves_all_tables_and_legacy_evidence(accounts, tmp_path):
    from trading_team.paper_recovery import restore_accounts, verify_backup
    from trading_team.paper_pipeline import account_snapshot

    before = {name: path.read_bytes() for name, path in paper_db_paths(accounts).items()}
    target = tmp_path / "backup"
    result = backup(accounts, target)
    manifest = verify_backup(target, result["manifest_hash"])
    assert manifest["scope"] == "paper_accounts_only"
    assert manifest["pair_alignment"] == "aligned"
    for item in manifest["accounts"].values():
        assert item["summary"]["unreceipted_days"] == ["2026-09-21"]
        assert item["summary"]["execution_evidence"] == "not_recertified"
        assert "execution_receipts" not in item["tables"]
    restored = tmp_path / "restored"
    outcome = restore_accounts(target, result["manifest_hash"], restored)
    assert outcome["status"] == "verified_isolated"
    assert outcome["execution_authorized"] is False
    assert not (restored / "trading_team/.runs/ledger.db").exists()
    for name, path in paper_db_paths(accounts).items():
        other = restored / "paper" / path.name
        assert account_snapshot(path, portfolio=name) == account_snapshot(other, portfolio=name)
        assert path.read_bytes() == before[name]
        with sqlite3.connect(other) as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_receipts'").fetchone()


def test_backup_includes_committed_wal_not_uncommitted_rows(accounts, tmp_path):
    path = paper_db_paths(accounts)["no_gate"]
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO account VALUES ('2026-09-22',80000,98000,'committed WAL')")
    writer.commit()
    writer.execute("UPDATE positions SET qty=400")
    try:
        target = tmp_path / "wal-backup"
        backup(accounts, target)
        with sqlite3.connect(target / "paper" / path.name) as conn:
            assert conn.execute("SELECT cash FROM account WHERE day='2026-09-22'").fetchone() == (80000,)
            assert conn.execute("SELECT qty FROM positions").fetchone() == (200,)
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("part", ["manifest.json", "paper/llm_gate.db", "paper/llm_no_gate.db"])
def test_tamper_rejected_before_restore_creates_destination(accounts, tmp_path, part):
    from trading_team.paper_recovery import restore_accounts
    source = tmp_path / "backup"
    result = backup(accounts, source)
    (source / part).write_bytes(b"damaged")
    target = tmp_path / "recovered"
    with pytest.raises(ValueError, match="hash|manifest|database"):
        restore_accounts(source, result["manifest_hash"], target)
    assert not target.exists()


def test_missing_database_is_not_initialized(accounts, tmp_path):
    path = paper_db_paths(accounts)["no_gate"]
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        backup(accounts, tmp_path / "backup")
    assert not path.exists()
    assert not (tmp_path / "backup/manifest.json").exists()


@pytest.mark.parametrize("operation", ["backup", "execute", "valuation", "legacy"])
def test_common_lock_excludes_all_public_writers(accounts, tmp_path, operation):
    from trading_team.paper_lock import account_locks
    from trading_team.paper_pipeline import run_paper_day, record_current_valuations, import_legacy_empty_receipts
    calls = {
        "backup": lambda: backup(accounts, tmp_path / "backup"),
        "execute": lambda: run_paper_day("2026-09-21", team=accounts),
        "valuation": lambda: record_current_valuations(team=accounts),
        "legacy": lambda: import_legacy_empty_receipts("2026-09-21", "a" * 64, {}, "run", accounts),
    }
    with account_locks(paper_db_paths(accounts).values()):
        with pytest.raises(RuntimeError, match="accounts busy"):
            calls[operation]()
    assert not (tmp_path / "backup/manifest.json").exists()


def test_failed_second_copy_never_publishes_success_and_preserves_partial(accounts, tmp_path, monkeypatch):
    from trading_team import paper_recovery
    original = paper_recovery._copy_database
    def fail_second(source, target):
        if target.name == "llm_no_gate.db":
            raise OSError("disk full")
        return original(source, target)
    monkeypatch.setattr(paper_recovery, "_copy_database", fail_second)
    target = tmp_path / "backup"
    with pytest.raises(OSError, match="disk full"):
        backup(accounts, target)
    assert (target / "paper/llm_gate.db").exists()
    assert not (target / "manifest.json").exists()
    with pytest.raises(ValueError, match="destination"):
        backup(accounts, target)


def test_restore_refuses_existing_or_source_location(accounts, tmp_path):
    from trading_team.paper_recovery import restore_accounts
    source = tmp_path / "backup"
    result = backup(accounts, source)
    for target in (accounts.parent, accounts.parent / "new", source / "new", tmp_path):
        with pytest.raises(ValueError, match="destination"):
            restore_accounts(source, result["manifest_hash"], target)
    assert not (accounts.parent / "new").exists()


def test_schema_and_rows_checked_not_just_physical_file_hash(accounts, tmp_path):
    from trading_team.paper_recovery import verify_backup
    target = tmp_path / "backup"
    backup(accounts, target)
    manifest_path = target / "manifest.json"
    document = json.loads(manifest_path.read_text())
    document["accounts"]["gate"]["tables"]["positions"]["count"] = 99
    manifest_path.write_text(json.dumps(document))
    pinned_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="content|summary"):
        verify_backup(target, pinned_hash)


def test_divergent_pair_preserved_but_not_labelled_aligned(accounts, tmp_path):
    from trading_team.paper_recovery import verify_backup
    with sqlite3.connect(paper_db_paths(accounts)["gate"]) as conn:
        conn.execute("INSERT INTO account VALUES ('2026-09-22',90000,100000,'partial pair')")
    target = tmp_path / "backup"
    result = backup(accounts, target)
    document = verify_backup(target, result["manifest_hash"])
    assert document["pair_alignment"] == "requires_reconciliation"


@pytest.mark.parametrize("damage", ["corrupt", "empty", "symlink"])
def test_invalid_source_not_backed_up_as_valid(accounts, tmp_path, damage):
    path = paper_db_paths(accounts)["gate"]
    if damage == "corrupt":
        path.write_bytes(b"not SQLite")
    elif damage == "empty":
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM account")
    else:
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
    with pytest.raises((ValueError, sqlite3.Error)):
        backup(accounts, tmp_path / "backup")
    assert not (tmp_path / "backup/manifest.json").exists()


def test_bypassing_writer_is_detected_without_publishing_manifest(accounts, tmp_path, monkeypatch):
    from trading_team import paper_recovery
    path = paper_db_paths(accounts)["gate"]
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    original = paper_recovery._copy_database
    def mutate_after_first_copy(source, target):
        original(source, target)
        if target.name == "llm_gate.db":
            writer.execute("UPDATE positions SET qty=400")
            writer.commit()
    monkeypatch.setattr(paper_recovery, "_copy_database", mutate_after_first_copy)
    try:
        target = tmp_path / "backup"
        with pytest.raises(ValueError, match="source changed"):
            backup(accounts, target)
        assert not (target / "manifest.json").exists()
    finally:
        writer.close()


def test_backup_refused_between_two_account_commits(accounts, tmp_path, monkeypatch, stub_research_reviews):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from trading_team import paper_pipeline

    day = "2026-09-22"
    plan = {"date": day, "data_asof": "2026-09-21", "proposals": []}
    output = accounts / "outputs" / day
    output.mkdir(parents=True)
    (output / "plan.json").write_text(json.dumps(plan))
    paper_pipeline.materialize_paper_decision(day, accounts)
    monkeypatch.setattr(paper_pipeline, "load_execution_bars", lambda *_: {})
    first_committed, release = Event(), Event()
    def execute(decision, name, path, bars, confirm):
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO account VALUES (?,90000,100000,'pair test')", (day,))
        if name == "gate":
            first_committed.set()
            assert release.wait(5)
        return {"status": "succeeded"}
    monkeypatch.setattr(paper_pipeline, "_execute_portfolio", execute)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(paper_pipeline.run_paper_day, day, accounts)
        try:
            assert first_committed.wait(5)
            with pytest.raises(RuntimeError, match="accounts busy"):
                backup(accounts, tmp_path / "backup")
        finally:
            release.set()
        future.result(timeout=5)
    assert backup(accounts, tmp_path / "complete-backup")["pair_alignment"] == "aligned"


def test_restored_execution_receipt_prevents_duplicate_fill(tmp_path):
    from paper.execution_model import Bar
    from trading_team.paper_pipeline import _execute_portfolio
    from trading_team.paper_recovery import restore_accounts
    team = tmp_path / "source/trading_team"
    team.mkdir(parents=True)
    decision = {"plan_hash": "version1", "data_asof": "2026-09-18", "eligible_execution_day": "2026-09-21",
                "portfolios": {name: {"plans": [{"code": "600036", "position_pct": 0.1,
                    "direction": "买入", "entry": [40], "entry_type": "limit", "executable": True}]} for name in ("gate", "no_gate")}}
    bars = {"600036": Bar(open=40, high=41, low=39, close=40.5, prev_close=40)}
    for name, path in paper_db_paths(team).items():
        assert _execute_portfolio(decision, name, path, bars, True)["fills"] == 1
    source = tmp_path / "backup"
    receipt = backup(team, source)
    target = tmp_path / "restored"
    restore_accounts(source, receipt["manifest_hash"], target)
    for name, path in paper_db_paths(target / "trading_team").items():
        before = path.read_bytes()
        assert _execute_portfolio(decision, name, path, bars, True)["status"] == "skipped"
        assert path.read_bytes() == before
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM trades").fetchone() == (1,)


def test_active_wal_sidecar_cannot_be_ignored_on_sealed_backup(accounts, tmp_path):
    from trading_team.paper_recovery import verify_backup
    source = tmp_path / "backup"
    result = backup(accounts, source)
    (source / "paper/llm_gate.db-wal").write_bytes(b"unverified WAL")
    with pytest.raises(ValueError, match="standalone"):
        verify_backup(source, result["manifest_hash"])


def test_restore_interruption_does_not_publish_restore_marker(accounts, tmp_path, monkeypatch):
    from trading_team import paper_recovery
    source = tmp_path / "backup"
    result = backup(accounts, source)
    original = paper_recovery.write_immutable
    def fail_second(path, content):
        if path.name == "llm_no_gate.db":
            raise OSError("disk full")
        original(path, content)
    monkeypatch.setattr(paper_recovery, "write_immutable", fail_second)
    target = tmp_path / "restored"
    with pytest.raises(OSError, match="disk full"):
        paper_recovery.restore_accounts(source, result["manifest_hash"], target)
    assert (target / "paper/llm_gate.db").exists()
    assert not (target / "restore.json").exists()


def test_cli_verification_fails_nonzero_for_wrong_pin(accounts, tmp_path, monkeypatch, capsys):
    from trading_team.paper_recovery import main
    source = tmp_path / "backup"
    backup(accounts, source)
    monkeypatch.setattr("sys.argv", ["paper_recovery", "verify", "--source", str(source),
                                    "--manifest-hash", "0" * 64])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
