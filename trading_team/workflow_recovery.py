"""Consistent workflow evidence snapshots and isolated, non-activating restoration."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3

from trading_team.execution_bars import SHANGHAI, write_immutable
from trading_team.paper_lock import account_locks
from trading_team.state_lock import state_lock
from trading_team.paper_pipeline import TEAM, active_paper_cohort, paper_db_paths
from trading_team.paper_recovery import (
    _copy_database, _describe, _hash, _json, _new_destination, _readonly, _sync_directory,
)

SCHEMA = "workflow_backup.v1"
SCOPE = "research_paper_workflow_evidence"
DATABASES = ("paper/llm_gate.db", "paper/llm_no_gate.db", "trading_team/.runs/ledger.db", "trading_team/team.db")
TREES = ("trading_team/context", "trading_team/outputs", "trading_team/decisions",
         "trading_team/replay_data", "trading_team/market_archive", "trading_team/prompts",
         "trading_team/loops", "trading_team/.runs/staging", "trading_team/.runs/archive",
         "trading_team/.runs/rejected", "trading_team/.runs/outbox",
         "trading_team/.runs/multica/reviews", "multica/runbooks", "docs/audits")
SINGLES = ("paper/active_cohort.json", "trading_team/watchlist.json", "trading_team/.runs/archive-service.json",
           "trading_team/.runs/multica/bootstrap.json", "multica/workspace.json", "AGENT.md", "LOOP_STATE.md",
           "docs/MULTICA_PRODUCTION_RUNBOOK.md")
EXCLUSIONS = ["credentials and environment files", "runtime code and dependencies", "host schedulers",
              "remote Multica resources", "existing backups", "logs and lock files", "legacy strategy accounts"]


def _database_paths(root: Path) -> tuple[str, ...]:
    cohort = active_paper_cohort(root / "trading_team")
    return DATABASES + (tuple(cohort["accounts"].values()) if cohort else ())


def _allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or path.as_posix() != relative
            or any(part in ("..", ".") or part.startswith(".") and part != ".runs" for part in path.parts)):
        return False
    if relative in SINGLES:
        return True
    if relative.startswith("docs/audits/"):
        return path.suffix == ".json"
    if re.fullmatch(r"trading_team/\.runs/\d{4}-\d{2}-\d{2}\.json", relative):
        return True
    legacy = re.match(r"trading_team/\.runs/previous-\d{4}-\d{2}-\d{2}/", relative)
    return bool((legacy or any(relative.startswith(tree + "/") for tree in TREES))
                and (path.suffix in (".json", ".md", ".npy") or path.name.endswith(".input.txt")))


def _safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("snapshot path escapes root or follows symlink")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("snapshot symlink is forbidden")
        if item == root:
            break
    return path


def _stamp(path: Path) -> tuple:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _inventory(root: Path) -> dict:
    paths = {root / name for name in SINGLES if (root / name).exists() or (root / name).is_symlink()}
    roots = [root / name for name in TREES]
    roots.extend((root / "trading_team/.runs").glob("previous-????-??-??"))
    for directory in roots:
        _safe_path(root, directory.relative_to(root).as_posix())
        if directory.exists():
            for path in directory.rglob("*"):
                if path.is_symlink():
                    raise ValueError("snapshot tree contains symlink")
                if path.is_file():
                    paths.add(path)
    paths.update((root / "trading_team/.runs").glob("????-??-??.json"))
    result = {}
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if not _allowed(relative):
            continue
        _safe_path(root, relative)
        stamp = _stamp(path)
        content = path.read_bytes()
        if stamp != _stamp(path):
            raise ValueError("workflow source changed during inventory")
        result[relative] = {"hash": _hash(content), "size": len(content), "stamp": stamp}
    return result


def _runtime(root: Path) -> dict:
    paths = [root / "console/serve.py", root / "console/index.html", root / "requirements.txt"]
    for directory in ("trading_team", "paper"):
        paths.extend((root / directory).glob("*.py"))
    return {path.relative_to(root).as_posix(): _hash(path.read_bytes()) for path in sorted(paths) if path.is_file()}


def _database_description(conn: sqlite3.Connection, relative: str) -> dict:
    if relative.startswith("paper/"):
        return _describe(conn)
    if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("workflow database integrity check failed")
    schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    tables = {}
    for kind, name, _, _ in schema:
        if kind != "table":
            continue
        identifier = '"' + name.replace('"', '""') + '"'
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({identifier})")]
        order = ','.join(str(i + 1) for i in range(len(columns)))
        digest = hashlib.sha256(_json(columns) + b"\n")
        count = 0
        for row in conn.execute(f"SELECT * FROM {identifier} ORDER BY {order}"):
            digest.update(_json(row) + b"\n")
            count += 1
        tables[name] = {"columns": columns, "count": count, "rows_hash": digest.hexdigest()}
    if relative.endswith("ledger.db") and not {"workflow_runs", "workflow_attempts", "step_runs"} <= tables.keys():
        raise ValueError("workflow ledger tables missing")
    return {"schema_hash": _hash(_json(schema)), "tables": tables}


def backup_workflow(*, team: Path = TEAM, target: Path) -> dict:
    if team.name != "trading_team" or not team.is_dir() or team.is_symlink():
        raise ValueError("workflow source must be a trading_team directory")
    root = team.parent.resolve()
    team = root / "trading_team"
    target = _new_destination(target, [root, TEAM.parent])
    started = datetime.now(SHANGHAI).isoformat()
    with ExitStack() as stack:
        stack.enter_context(state_lock(team))
        # Physical locks, not stale ledger statuses, establish whether a known writer owns the files.
        for path in sorted((team / ".runs/locks").glob("*.lock")):
            if path.name == "shared-state.lock":
                continue
            _safe_path(root, path.relative_to(root).as_posix())
            handle = stack.enter_context(path.open("rb"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("workflow writer busy; retry after the active operation") from exc
        database_paths = _database_paths(root)
        stack.enter_context(account_locks(root / path for path in database_paths if path.startswith("paper/")
                                          and (root / path).exists()))
        connections, versions, identities, descriptions = {}, {}, {}, {}
        missing = []
        for relative in database_paths:
            path = _safe_path(root, relative)
            if not path.exists():
                missing.append(relative)
                continue
            conn = stack.enter_context(closing(_readonly(path)))
            connections[relative] = conn
            versions[relative] = conn.execute("PRAGMA data_version").fetchone()[0]
            identities[relative] = _stamp(path)[:2]
        if "trading_team/.runs/ledger.db" in missing:
            raise ValueError("workflow ledger missing; no workflow snapshot available")
        if active_paper_cohort(team) and any(path in missing for path in active_paper_cohort(team)["accounts"].values()):
            raise ValueError("active Paper cohort account missing")
        files = _inventory(root)
        runtime = _runtime(root)
        for relative, conn in connections.items():
            conn.execute("BEGIN")
            descriptions[relative] = _database_description(conn, relative)
        target.mkdir(parents=True, mode=0o700)
        for relative, conn in connections.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _copy_database(conn, path)
            with closing(_readonly(path, immutable=True)) as copy:
                if _database_description(copy, relative) != descriptions[relative]:
                    raise ValueError("workflow database copy differs from snapshot")
            descriptions[relative]["file_hash"] = _hash(path.read_bytes())
        for relative, facts in files.items():
            source = _safe_path(root, relative)
            try:
                content = source.read_bytes()
            except FileNotFoundError as exc:
                raise ValueError("workflow source changed during backup") from exc
            if _hash(content) != facts["hash"] or _stamp(source) != facts["stamp"]:
                raise ValueError("workflow source changed during backup")
            write_immutable(target / relative, content)
            (target / relative).chmod(0o600)
        if files != _inventory(root) or runtime != _runtime(root):
            raise ValueError("workflow source changed during backup")
        for relative in missing:
            if (root / relative).exists():
                raise ValueError("workflow database inventory changed during backup")
        for relative, conn in connections.items():
            conn.rollback()
            if (conn.execute("PRAGMA data_version").fetchone()[0] != versions[relative]
                    or _stamp(root / relative)[:2] != identities[relative]):
                raise ValueError("workflow database changed during backup")
        manifest = {"schema": SCHEMA, "scope": SCOPE, "source_root": str(root),
                    "started_at": started, "completed_at": datetime.now(SHANGHAI).isoformat(),
                    "execution_authorized": False, "excluded": EXCLUSIONS,
                    "runtime_fingerprints": runtime, "missing_databases": missing,
                    "files": {name: {key: row[key] for key in ("hash", "size")} for name, row in files.items()},
                    "databases": descriptions, "execution_evidence": "not_recertified"}
        content = _json(manifest)
        _verify_payload(target, manifest)
        write_immutable(target / "manifest.json", content)
        _sync_directory(target.parent)
    return {"status": "verified", "scope": SCOPE, "path": str(target), "manifest_hash": _hash(content),
            "file_count": len(files), "database_count": len(descriptions), "execution_authorized": False}


def _verify_payload(source: Path, manifest: dict) -> None:
    files, databases, missing = manifest["files"], manifest["databases"], manifest["missing_databases"]
    expected_databases = _database_paths(source)
    if active_paper_cohort(source / "trading_team") and "paper/active_cohort.json" not in files:
        raise ValueError("active Paper cohort configuration missing from inventory")
    if (not isinstance(files, dict) or not isinstance(databases, dict) or not isinstance(missing, list)
            or set(databases) & set(missing) or set(databases) | set(missing) != set(expected_databases)
            or len(set(missing)) != len(missing) or "trading_team/.runs/ledger.db" not in databases):
        raise ValueError("invalid workflow inventory")
    cohort = active_paper_cohort(source / "trading_team")
    if cohort and any(path not in databases for path in cohort["accounts"].values()):
        raise ValueError("active Paper cohort account missing from backup")
    for relative, facts in files.items():
        if not isinstance(relative, str) or not _allowed(relative):
            raise ValueError("workflow file path outside contract")
        path = _safe_path(source, relative)
        content = path.read_bytes()
        if _hash(content) != facts["hash"] or len(content) != facts["size"]:
            raise ValueError("workflow file hash mismatch")
    for relative, facts in databases.items():
        path = _safe_path(source, relative)
        if _hash(path.read_bytes()) != facts["file_hash"]:
            raise ValueError("workflow database hash mismatch")
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal", "-shm")):
            raise ValueError("workflow backup database must be standalone")
        with closing(_readonly(path, immutable=True)) as conn:
            if _database_description(conn, relative) != {key: value for key, value in facts.items() if key != "file_hash"}:
                raise ValueError("workflow database contents mismatch")
    actual = set()
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("workflow backup symlink forbidden")
        if path.is_file():
            actual.add(path.relative_to(source).as_posix())
    if actual - {"manifest.json", "restore.json"} != set(files) | set(databases):
        raise ValueError("workflow payload inventory mismatch")


def verify_workflow_backup(source: Path, manifest_hash: str) -> dict:
    try:
        if not isinstance(manifest_hash, str) or re.fullmatch(r"[0-9a-f]{64}", manifest_hash) is None:
            raise ValueError("invalid workflow manifest hash")
        content = _safe_path(source, "manifest.json").read_bytes()
        if _hash(content) != manifest_hash:
            raise ValueError("workflow manifest hash mismatch")
        manifest = json.loads(content)
        if (manifest["schema"] != SCHEMA or manifest["scope"] != SCOPE
                or not isinstance(manifest.get("source_root"), str)
                or not Path(manifest["source_root"]).is_absolute()
                or manifest["execution_authorized"] is not False or manifest["execution_evidence"] != "not_recertified"):
            raise ValueError("invalid workflow manifest contract")
        _verify_payload(source, manifest)
        return manifest
    except (OSError, KeyError, TypeError, sqlite3.Error) as exc:
        raise ValueError(f"invalid workflow backup: {exc}") from exc


def restore_workflow(source: Path, manifest_hash: str, target: Path) -> dict:
    manifest = verify_workflow_backup(source, manifest_hash)
    target = _new_destination(target, [source, Path(manifest["source_root"]), TEAM.parent])
    target.mkdir(parents=True, mode=0o700)
    for relative, facts in {**manifest["files"], **manifest["databases"]}.items():
        content = _safe_path(source, relative).read_bytes()
        if _hash(content) != facts.get("hash", facts.get("file_hash")):
            raise ValueError("workflow backup changed during restore")
        write_immutable(target / relative, content)
        (target / relative).chmod(0o600)
    content = (source / "manifest.json").read_bytes()
    if _hash(content) != manifest_hash:
        raise ValueError("workflow manifest changed during restore")
    write_immutable(target / "manifest.json", content)
    verify_workflow_backup(target, manifest_hash)
    result = {"status": "verified_isolated", "scope": SCOPE, "path": str(target),
              "manifest_hash": manifest_hash, "restored_at": datetime.now(SHANGHAI).isoformat(),
              "execution_authorized": False, "execution_evidence": "not_recertified",
              "activation": "manual_review_required_no_runtime_or_scheduler_installed"}
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
            result = backup_workflow(team=args.team, target=args.target)
        elif args.command == "restore":
            result = restore_workflow(args.source, args.manifest_hash, args.target)
        else:
            result = verify_workflow_backup(args.source, args.manifest_hash)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
