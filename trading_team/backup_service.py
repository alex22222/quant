"""Host-bound, model-free workflow snapshots with a persistent receipt catalog."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from trading_team.execution_bars import SHANGHAI, TEAM
from trading_team.paper_pipeline import active_paper_cohort
from trading_team.state_lock import atomic_write_json, state_lock
from trading_team.workflow_recovery import (
    _database_paths, _inventory, _runtime, backup_workflow, verify_workflow_backup,
)

SCHEMA = "workflow_backup_catalog.v1"
CHECK_SECONDS = 900
SNAPSHOT_SECONDS = 21600
MAX_STORE_BYTES = 5 * 1024 ** 3
MIN_FREE_BYTES = 1024 ** 3
READ_ERRORS = (OSError, ValueError, TypeError, KeyError, sqlite3.Error)


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("backup timestamp requires timezone")
    return result


def _safe(path: Path) -> Path:
    path = path.absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("backup paths cannot contain symlinks")
    return path.resolve()


def _store_path(team: Path, store: Path) -> Path:
    store = _safe(store)
    for root in (team.parent.resolve(), TEAM.parent.resolve()):
        if store.is_relative_to(root) or root.is_relative_to(store):
            raise ValueError("backup store must be outside the source project")
    return store


def _object(path: Path) -> dict:
    value = json.loads(_safe(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("backup state must be an object")
    return value


def _config(team: Path) -> dict:
    config = _object(team / ".runs/backup-service.json")
    if config.get("schema_version") != SCHEMA or config.get("source_root") != str(team.parent.resolve()):
        raise ValueError("backup configuration source binding mismatch")
    store = _store_path(team, Path(config["store_root"]))
    if str(store) != config["store_root"]:
        raise ValueError("backup store binding mismatch")
    return config


def _catalog(config: dict) -> dict:
    store = Path(config["store_root"])
    try:
        value = _object(store / "catalog.json")
    except OSError as exc:
        raise ValueError("configured backup catalog unavailable; manual investigation required") from exc
    if any(value.get(key) != config.get(key) for key in ("schema_version", "source_root", "store_root", "store_id")):
        raise ValueError("backup catalog binding mismatch")
    attempts = value.get("attempts")
    if not isinstance(attempts, list) or not isinstance(value.get("integrity_failures"), list):
        raise ValueError("backup catalog collections invalid")
    ids = set()
    for item in attempts:
        if (not isinstance(item, dict) or not re.fullmatch(r"[a-f0-9]{32}", item.get("id", ""))
                or item["id"] in ids or item.get("status") not in ("running", "succeeded", "failed", "interrupted")):
            raise ValueError("backup catalog attempt invalid")
        ids.add(item["id"])
        _time(item["started_at"])
        if item.get("path") != str(store / "snapshots" / item["id"]):
            raise ValueError("backup catalog snapshot path mismatch")
        if item["status"] == "succeeded":
            if not re.fullmatch(r"[a-f0-9]{64}", item.get("manifest_hash", "")):
                raise ValueError("backup catalog pin invalid")
            _time(item["finished_at"])
    return value


def _save_catalog(store: Path, catalog: dict) -> None:
    _safe(store / "catalog.json")
    atomic_write_json(store / "catalog.json", catalog)


def initialize(*, team: Path = TEAM, store: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(SHANGHAI)
    team = _safe(team)
    if team.name != "trading_team" or not team.is_dir():
        raise ValueError("backup source must be a trading_team directory")
    store = _store_path(team, store)
    path = _safe(team / ".runs/backup-service.json")
    with state_lock(team):
        if path.exists() or path.is_symlink():
            config = _config(team)
            if config["store_root"] != str(store):
                raise ValueError("existing backup store differs; explicit migration required")
            _catalog(config)
            return {"status": "configured", **config}
        if store.exists() and (not store.is_dir() or any(store.iterdir())):
            raise ValueError("refusing to adopt a nonempty unowned backup store")
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
        store.chmod(0o700)
        (store / "snapshots").mkdir(mode=0o700)
        config = {"schema_version": SCHEMA, "source_root": str(team.parent), "store_root": str(store),
                  "store_id": uuid.uuid4().hex, "initialized_at": now.isoformat()}
        _save_catalog(store, {**config, "attempts": [], "integrity_failures": [], "last_check_at": None})
        atomic_write_json(path, config)
        return {"status": "configured", **config}


def _latest(catalog: dict) -> dict | None:
    return next((item for item in reversed(catalog["attempts"]) if item["status"] == "succeeded"), None)


def _verify(item: dict, catalog: dict) -> dict:
    result = {**item, "verified": False}
    try:
        manifest = verify_workflow_backup(_safe(Path(item["path"])), item["manifest_hash"])
        if manifest["source_root"] != catalog["source_root"]:
            raise ValueError("snapshot source binding mismatch")
        result["verified"] = True
        result["cohort_config_hash"] = manifest["files"].get("paper/active_cohort.json", {}).get("hash")
        try:
            root = Path(catalog["source_root"])
            result["runtime_snapshot_current"] = _runtime(root) == manifest["runtime_fingerprints"]
            prompts = _safe(root / "trading_team/prompts")
            current = {path.relative_to(root).as_posix(): hashlib.sha256(_safe(path).read_bytes()).hexdigest()
                       for path in prompts.rglob("*") if path.is_file()}
            recorded = {name: item["hash"] for name, item in manifest["files"].items()
                        if name.startswith("trading_team/prompts/")}
            result["prompts_snapshot_current"] = current == recorded
            runbook = _safe(root / "docs/MULTICA_PRODUCTION_RUNBOOK.md")
            result["runbook_snapshot_current"] = (
                manifest["files"].get("docs/MULTICA_PRODUCTION_RUNBOOK.md", {}).get("hash")
                == hashlib.sha256(runbook.read_bytes()).hexdigest()
            )
        except READ_ERRORS as exc:
            result["source_check_error"] = str(exc)
    except READ_ERRORS as exc:
        result["error"] = str(exc)
    return result


def _usage(store: Path) -> int:
    total = 0
    for path in store.rglob("*"):
        if path.is_symlink():
            raise ValueError("backup store contains symlink")
        if path.is_file():
            total += path.stat().st_size
    return total


def _report(config: dict, catalog: dict, now: datetime) -> dict:
    store = Path(config["store_root"])
    issues = [f"retained integrity failure: {item['id']}: {item['error']}" for item in catalog["integrity_failures"]]
    latest = _latest(catalog)
    latest = _verify(latest, catalog) if latest else None
    team = Path(config["source_root"]) / "trading_team"
    cohort = active_paper_cohort(team)
    cohort_path = team.parent / "paper/active_cohort.json"
    cohort_hash = hashlib.sha256(cohort_path.read_bytes()).hexdigest() if cohort else None
    cohort_snapshot_current = bool(latest and latest["verified"]
                                   and latest.get("cohort_config_hash") == cohort_hash)
    if not latest:
        issues.append("no acknowledged snapshot")
    elif not latest["verified"]:
        issues.append("latest snapshot invalid: " + latest["error"])
    elif not cohort_snapshot_current:
        issues.append("active Paper cohort not in latest snapshot")
    elif (now - _time(latest["started_at"])).total_seconds() > SNAPSHOT_SECONDS + CHECK_SECONDS:
        issues.append("latest snapshot overdue")
    if latest and latest["verified"]:
        if latest.get("runtime_snapshot_current") is not True:
            issues.append("runtime differs from latest snapshot")
        if latest.get("prompts_snapshot_current") is not True:
            issues.append("prompts differ from latest snapshot")
        if latest.get("runbook_snapshot_current") is not True:
            issues.append("production runbook differs from latest snapshot")
    heartbeat = "missing"
    if catalog.get("last_check_at"):
        age = (now - _time(catalog["last_check_at"])).total_seconds()
        heartbeat = "future" if age < 0 else "stale" if age > CHECK_SECONDS * 2 else "fresh"
    if heartbeat != "fresh":
        issues.append("backup checker heartbeat " + heartbeat)
    attempts = catalog["attempts"]
    if attempts and attempts[-1]["status"] != "succeeded":
        issues.append("latest attempt " + attempts[-1]["status"] + ": " + attempts[-1].get("error", "acknowledgement missing"))
    if any(_time(item["started_at"]) > now for item in attempts):
        issues.append("backup attempt timestamp is in the future")
    acknowledged = {item["path"] for item in attempts if item["status"] == "succeeded"}
    unregistered = [str(path) for path in _safe(store / "snapshots").iterdir() if str(path) not in acknowledged]
    if unregistered:
        issues.append(f"{len(unregistered)} unregistered or partial snapshots retained")
    used = _usage(store)
    free = shutil.disk_usage(store).free
    if used >= MAX_STORE_BYTES or free < MIN_FREE_BYTES:
        issues.append("backup storage capacity requires operator action; no automatic deletion")
    if catalog.get("last_error"):
        issues.append(catalog["last_error"])
    return {"schema_version": SCHEMA, "status": "degraded" if issues else "healthy",
            "store_root": str(store), "source_root": config["source_root"], "latest": latest,
            "snapshot_count": len(acknowledged), "attempt_count": len(attempts),
            "unregistered_count": len(unregistered), "issues": issues, "heartbeat": heartbeat,
            "last_check_at": catalog.get("last_check_at"), "trigger_source": catalog.get("trigger_source"),
            "verification_scope": "latest_snapshot_only", "storage_bytes": used, "free_bytes": free,
            "max_store_bytes": MAX_STORE_BYTES, "snapshot_interval_seconds": SNAPSHOT_SECONDS,
            "offsite_verified": False, "execution_authorized": False,
            "cohort_snapshot_current": cohort_snapshot_current,
            "runtime_snapshot_current": bool(latest and latest.get("runtime_snapshot_current")),
            "prompts_snapshot_current": bool(latest and latest.get("prompts_snapshot_current")),
            "runbook_snapshot_current": bool(latest and latest.get("runbook_snapshot_current"))}


def service_status(*, team: Path = TEAM, now: datetime | None = None) -> dict:
    now = now or datetime.now(SHANGHAI)
    path = team / ".runs/backup-service.json"
    base = {"offsite_verified": False, "execution_authorized": False}
    if not path.exists() and not path.is_symlink():
        return {**base, "status": "uninitialized"}
    try:
        config = _config(team)
        return _report(config, _catalog(config), now)
    except READ_ERRORS as exc:
        return {**base, "status": "invalid", "issues": [str(exc)]}


def _estimated_bytes(team: Path) -> int:
    size = sum(item["size"] for item in _inventory(team.parent).values())
    for relative in _database_paths(team.parent):
        for suffix in ("", "-wal"):
            path = team.parent / (relative + suffix)
            if path.is_file():
                size += path.stat().st_size
    return size * 2 + 1024 ** 2


def tick(*, team: Path = TEAM, now: datetime | None = None, trigger_source: str = "manual") -> dict:
    now = now or datetime.now(SHANGHAI)
    config = _config(team)
    store = Path(config["store_root"])
    with state_lock(team):
        catalog = _catalog(config)
        report = _report(config, catalog, now)
        if report["heartbeat"] == "future" or any(_time(a["started_at"]) > now for a in catalog["attempts"]):
            raise ValueError("backup clock moved backwards; operator review required")
        latest = report["latest"]
        runbook = _safe(team.parent / "docs/MULTICA_PRODUCTION_RUNBOOK.md")
        if latest and not latest["verified"] and not any(a["id"] == latest["id"] for a in catalog["integrity_failures"]):
            catalog["integrity_failures"].append({"id": latest["id"], "detected_at": now.isoformat(), "error": latest["error"]})
        # Owning the shared lock proves no cooperating backup is still writing this attempt.
        for attempt in catalog["attempts"]:
            if attempt["status"] == "running":
                attempt.update(status="interrupted", error="process ended before catalog acknowledgement")
        catalog.update(last_check_at=now.isoformat(), trigger_source=trigger_source, last_error=None)
        due = (not latest or not latest["verified"] or not report["cohort_snapshot_current"]
               or (runbook.is_file() and not report["runbook_snapshot_current"])
               or (now - _time(latest["started_at"])).total_seconds() >= SNAPSHOT_SECONDS)
        previous = catalog["attempts"][-1] if catalog["attempts"] else None
        retry_ready = not previous or (now - _time(previous["started_at"])).total_seconds() >= CHECK_SECONDS
        created = False
        if due and retry_ready:
            estimate = _estimated_bytes(team)
            if report["storage_bytes"] + estimate > MAX_STORE_BYTES or report["free_bytes"] - estimate < MIN_FREE_BYTES:
                catalog["last_error"] = "insufficient backup capacity; existing snapshots retained"
                _save_catalog(store, catalog)
            else:
                identity = uuid.uuid4().hex
                attempt = {"id": identity, "started_at": now.isoformat(), "status": "running",
                           "path": str(store / "snapshots" / identity)}
                catalog["attempts"].append(attempt)
                _save_catalog(store, catalog)
                try:
                    result = backup_workflow(team=team, target=Path(attempt["path"]))
                    attempt.update(manifest_hash=result["manifest_hash"])
                    verified = _verify(attempt, catalog)
                    if not verified["verified"]:
                        raise ValueError(verified["error"])
                    attempt.update(status="succeeded", finished_at=now.isoformat())
                    created = True
                except (*READ_ERRORS, RuntimeError) as exc:
                    attempt.update(status="failed", finished_at=now.isoformat(), error=str(exc))
        _save_catalog(store, catalog)
        return {**_report(config, catalog, now), "created": created}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "tick", "status"))
    parser.add_argument("--team", type=Path, default=TEAM)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--trigger-source", default="manual")
    args = parser.parse_args()
    try:
        if args.command == "init":
            if not args.store:
                raise ValueError("--store is required for initialization")
            result = initialize(team=args.team, store=args.store)
        elif args.command == "tick":
            result = tick(team=args.team, trigger_source=args.trigger_source)
        else:
            result = service_status(team=args.team)
    except (*READ_ERRORS, RuntimeError) as exc:
        result = {"status": "failed", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] in ("healthy", "configured") else 1)


if __name__ == "__main__":
    main()
