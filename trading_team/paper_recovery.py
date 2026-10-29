"""Verified dual-account snapshots and isolated restoration, never in-place recovery."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from trading_team.execution_bars import SHANGHAI, write_immutable
from trading_team.paper_lock import account_locks
from trading_team.paper_pipeline import TEAM, paper_db_paths

SCHEMA = "paper_backup.v1"
FILES = {"gate": "paper/llm_gate.db", "no_gate": "paper/llm_no_gate.db"}


def _json(value) -> bytes:
    def encode(cell):
        if isinstance(cell, bytes):
            return {"blob_hex": cell.hex()}
        raise TypeError(f"unsupported backup value: {type(cell).__name__}")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      default=encode).encode()


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Paper database missing or symlink: {path.name}")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True)


def _describe(conn: sqlite3.Connection) -> dict:
    if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("Paper database integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Paper database foreign key check failed")
    schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    names = [row[1] for row in schema if row[0] == "table"]
    if not {"account", "positions", "pending_orders", "trades"} <= set(names):
        raise ValueError("Paper account tables missing")
    tables = {}
    for name in names:
        identifier = '"' + name.replace('"', '""') + '"'
        cursor = conn.execute(f"SELECT rowid,* FROM {identifier} ORDER BY rowid")
        columns = [column[0] for column in cursor.description]
        digest = hashlib.sha256(_json(columns) + b"\n")
        count = 0
        for row in cursor:
            digest.update(_json(row) + b"\n")
            count += 1
        tables[name] = {"count": count, "columns": columns, "rows_hash": digest.hexdigest()}
    latest = conn.execute("SELECT day,cash,equity FROM account ORDER BY rowid DESC LIMIT 1").fetchone()
    if latest is None:
        raise ValueError("Paper account balance missing")
    days = {row[0] for row in conn.execute("SELECT day FROM account WHERE day!='init'")}
    days.update(row[0] for row in conn.execute("SELECT execution_day FROM pending_orders"))
    days.update(row[0] for row in conn.execute("SELECT day FROM trades"))
    receipts = ({row[0] for row in conn.execute("SELECT execution_day FROM execution_receipts")}
                if "execution_receipts" in names else set())
    summary = {"last_account_day": latest[0], "cash": latest[1], "recorded_equity": latest[2],
               "position_count": tables["positions"]["count"],
               "unreceipted_days": sorted(days - receipts - {None}),
               "execution_evidence": "not_recertified"}
    return {"schema_hash": _hash(_json(schema)), "tables": tables, "summary": summary}


def _copy_database(source: sqlite3.Connection, target: Path) -> None:
    deadline = time.monotonic() + 30
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("Paper backup timed out")
    with closing(sqlite3.connect(target)) as dest:
        source.backup(dest, pages=128, progress=progress, sleep=0.05)
        dest.execute("PRAGMA journal_mode=DELETE")
    target.chmod(0o600)
    with target.open("rb") as handle:
        os.fsync(handle.fileno())


def _new_destination(target: Path, forbidden: list[Path]) -> Path:
    resolved = target.resolve()
    if target.exists() or target.is_symlink() or any(resolved.is_relative_to(path.resolve()) for path in forbidden):
        raise ValueError("recovery destination must be new and outside protected source locations")
    return resolved


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _alignment(accounts: dict) -> str:
    days = {item["summary"]["last_account_day"] for item in accounts.values()}
    return "aligned" if len(days) == 1 else "requires_reconciliation"


def backup_accounts(*, team: Path = TEAM, target: Path) -> dict:
    paths = paper_db_paths(team)
    target = _new_destination(target, [team.parent / "paper"])
    for path in paths.values():
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
            raise ValueError(f"Paper database missing or symlink: {path.name}")
    started = datetime.now(SHANGHAI).isoformat()
    with account_locks(paths.values()), ExitStack() as stack:
        connections = {name: stack.enter_context(closing(_readonly(path))) for name, path in paths.items()}
        versions, identities, descriptions = {}, {}, {}
        for name, conn in connections.items():
            versions[name] = conn.execute("PRAGMA data_version").fetchone()[0]
            info = paths[name].stat()
            identities[name] = (info.st_dev, info.st_ino)
            conn.execute("BEGIN")
            descriptions[name] = _describe(conn)
        target.mkdir(parents=True, mode=0o700)
        (target / "paper").mkdir(mode=0o700)
        for name, conn in connections.items():
            path = target / FILES[name]
            _copy_database(conn, path)
            with closing(_readonly(path, immutable=True)) as dest:
                if _describe(dest) != descriptions[name]:
                    raise ValueError("Paper backup content differs from read snapshot")
            descriptions[name].update(file=FILES[name], file_hash=_hash(path.read_bytes()))
        # Detect writers that bypass the shared application locks during the snapshot window.
        for name, conn in connections.items():
            conn.rollback()
            info = paths[name].stat()
            if (conn.execute("PRAGMA data_version").fetchone()[0] != versions[name]
                    or (info.st_dev, info.st_ino) != identities[name]):
                raise ValueError("Paper source changed during backup; retry with writers quiescent")
        manifest = {"schema": SCHEMA, "scope": "paper_accounts_only", "source_root": str(team.parent.resolve()),
                    "started_at": started, "completed_at": datetime.now(SHANGHAI).isoformat(),
                    "accounts": descriptions, "pair_alignment": _alignment(descriptions),
                    "execution_authorized": False}
        content = _json(manifest)
        _sync_directory(target / "paper")
        write_immutable(target / "manifest.json", content)
        _sync_directory(target.parent)
    manifest_hash = _hash(content)
    verify_backup(target, manifest_hash)
    return {"status": "verified", "scope": manifest["scope"], "path": str(target),
            "manifest_hash": manifest_hash, "pair_alignment": manifest["pair_alignment"]}


def verify_backup(source: Path, manifest_hash: str) -> dict:
    if re.fullmatch(r"[0-9a-f]{64}", manifest_hash) is None:
        raise ValueError("invalid manifest hash")
    try:
        path = source / "manifest.json"
        content = path.read_bytes()
        if path.is_symlink() or _hash(content) != manifest_hash:
            raise ValueError("Paper manifest hash mismatch")
        manifest = json.loads(content)
        if (manifest["schema"] != SCHEMA or manifest["scope"] != "paper_accounts_only"
                or manifest["execution_authorized"] is not False or set(manifest["accounts"]) != set(FILES)):
            raise ValueError("invalid Paper manifest contract")
        for name, relative in FILES.items():
            item = manifest["accounts"][name]
            path = source / relative
            if (item["file"] != relative or path.is_symlink() or path.parent.is_symlink()
                    or not path.resolve().is_relative_to(source.resolve())
                    or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal"))
                    or _hash(path.read_bytes()) != item["file_hash"]):
                raise ValueError("Paper database file hash or standalone contract mismatch")
            with closing(_readonly(path, immutable=True)) as conn:
                if _describe(conn) != {key: item[key] for key in ("schema_hash", "tables", "summary")}:
                    raise ValueError("Paper backup content or summary mismatch")
        if manifest["pair_alignment"] != _alignment(manifest["accounts"]):
            raise ValueError("Paper pair summary mismatch")
        return manifest
    except (OSError, KeyError, TypeError, sqlite3.Error, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Paper backup manifest or database: {exc}") from exc


def restore_accounts(source: Path, manifest_hash: str, target: Path) -> dict:
    manifest = verify_backup(source, manifest_hash)
    target = _new_destination(target, [source, Path(manifest["source_root"]), TEAM.parent])
    target.mkdir(parents=True, mode=0o700)
    for name, relative in FILES.items():
        content = (source / relative).read_bytes()
        if _hash(content) != manifest["accounts"][name]["file_hash"]:
            raise ValueError("Paper backup database changed during restore")
        write_immutable(target / relative, content)
        (target / relative).chmod(0o600)
    content = (source / "manifest.json").read_bytes()
    if _hash(content) != manifest_hash:
        raise ValueError("Paper backup manifest changed during restore")
    write_immutable(target / "manifest.json", content)
    verify_backup(target, manifest_hash)
    result = {"status": "verified_isolated", "scope": "paper_accounts_only", "execution_authorized": False,
              "manifest_hash": manifest_hash, "restored_at": datetime.now(SHANGHAI).isoformat(),
              "path": str(target), "pair_alignment": manifest["pair_alignment"]}
    write_immutable(target / "restore.json", _json(result))
    _sync_directory(target.parent)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--team", type=Path, default=TEAM)
    backup.add_argument("--target", type=Path, required=True)
    for name in ("verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--manifest-hash", required=True)
        if name == "restore":
            command.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            result = backup_accounts(team=args.team, target=args.target)
        elif args.command == "restore":
            result = restore_accounts(args.source, args.manifest_hash, args.target)
        else:
            result = verify_backup(args.source, args.manifest_hash)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
