"""Model-free closing evidence capture and isolated, offline source recovery."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, time
from pathlib import Path

from trading_team.collect import DEFAULT_TRADING_CALENDAR, load_trading_days
from trading_team.execution_bars import (
    SHANGHAI, TEAM, _cache_document, _hash, load_execution_bars,
    validate_document, write_immutable,
)
from trading_team.run_ledger import RunLedger

SCHEMA = "market_archive.v1"


def _digest(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid archive hash")
    return value


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      indent=2).encode("utf-8")


def completed_day(business_day: str, calendar_path: Path, now: datetime | None = None) -> str:
    if date.fromisoformat(business_day).isoformat() != business_day:
        raise ValueError("noncanonical business date")
    current = now or datetime.now(SHANGHAI)
    current = current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    today = current.date().isoformat()
    if business_day > today:
        raise ValueError("future business date")
    sessions = load_trading_days(calendar_path)
    if not sessions or not min(sessions) <= business_day <= max(sessions):
        raise ValueError(f"calendar does not cover {business_day}")
    completed = [day for day in sessions if day <= business_day and (
        day < today or current.time() >= time(15, 10))]
    if not completed:
        raise ValueError("calendar has no completed session")
    return max(completed)


def required_symbols(team: Path) -> list[str]:
    from trading_team.paper_pipeline import paper_db_paths

    watchlist = json.loads((team / "watchlist.json").read_text(encoding="utf-8"))
    codes = {item["code"] for item in watchlist["stocks"]} | {"000300"}
    for path in paper_db_paths(team).values():
        if not path.exists():
            continue
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            codes.update(row[0] for row in conn.execute("SELECT code FROM positions WHERE qty > 0"))
        finally:
            conn.close()
    if any(not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None for code in codes):
        raise ValueError("invalid archive symbol")
    return sorted(codes)


def _receipt_objects(team: Path, receipt: dict) -> tuple[Path, Path, Path]:
    day = receipt["date"]
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("invalid archive date")
    root = team / "market_archive"
    return (root / day / f"{_digest(receipt['payload_hash'])}.json",
            root / "calendars" / f"{_digest(receipt['calendar_hash'])}.npy",
            root / "receipts" / f"{_digest(receipt['receipt_hash'])}.json")


def verify_receipt(team: Path, receipt: dict) -> dict:
    try:
        if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
            raise ValueError("archive receipt schema missing")
        content = {key: value for key, value in receipt.items() if key != "receipt_hash"}
        if receipt.get("receipt_hash") != _hash(content):
            raise ValueError("archive receipt hash mismatch")
        data_path, calendar_path, receipt_path = _receipt_objects(team, receipt)
        for path in (data_path, calendar_path, receipt_path):
            if not path.resolve().is_relative_to((team / "market_archive").resolve()) or path.is_symlink():
                raise ValueError("archive object path escapes archive")
        if json.loads(receipt_path.read_bytes()) != receipt:
            raise ValueError("archive receipt object mismatch")
        if hashlib.sha256(calendar_path.read_bytes()).hexdigest() != receipt["calendar_hash"]:
            raise ValueError("archive calendar hash mismatch")
        if receipt["date"] not in load_trading_days(calendar_path):
            raise ValueError("archive date outside calendar")
        document = validate_document(receipt["date"], json.loads(data_path.read_bytes()))
        if document["payload_hash"] != receipt["payload_hash"]:
            raise ValueError("archive payload hash mismatch")
        if not document["bars"] or receipt.get("codes") != sorted(document["bars"]):
            raise ValueError("archive symbol coverage mismatch")
        return document
    except (OSError, KeyError, TypeError, ValueError, EOFError) as exc:
        raise ValueError(f"invalid market archive: {exc}") from exc


def _capture_closing(business_day: str, *, team: Path, calendar_path: Path,
                     now: datetime | None, ledger: RunLedger | None, trigger_source: str) -> dict:
    ledger = ledger or RunLedger(team / ".runs/ledger.db")
    run_id = ledger.start_run("closing_archive", business_day, trigger_source=trigger_source,
                              input_hash=_hash({"schema": SCHEMA, "date": business_day}))
    step_id = ledger.start_step(run_id, "capture_and_verify")
    try:
        calendar = calendar_path.read_bytes()
        calendar_hash = hashlib.sha256(calendar).hexdigest()
        frozen_calendar = team / "market_archive/calendars" / f"{calendar_hash}.npy"
        write_immutable(frozen_calendar, calendar)
        day = completed_day(business_day, frozen_calendar, now)
        codes = required_symbols(team)
        load_execution_bars(day, codes, team, calendar_path=frozen_calendar, now=now)
        document = _cache_document(day, team)
        content = {"schema": SCHEMA, "date": day, "payload_hash": document["payload_hash"],
                   "calendar_hash": calendar_hash, "codes": sorted(document["bars"])}
        receipt = {**content, "receipt_hash": _hash(content)}
        receipt_path = _receipt_objects(team, receipt)[2]
        write_immutable(receipt_path, _json_bytes(receipt))
        verify_receipt(team, receipt)
        ledger.finish_step(step_id, output_hash=receipt["receipt_hash"],
                           artifact_path=str(receipt_path.relative_to(team.parent)), metadata=receipt)
        ledger.finish_run(run_id, status="succeeded", data_asof=day, metadata=receipt)
        return receipt
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        ledger.finish_step(step_id, status="failed", error=error)
        ledger.finish_run(run_id, status="failed", error=error)
        raise


def capture_closing(business_day: str, *, team: Path = TEAM,
                    calendar_path: Path = DEFAULT_TRADING_CALENDAR,
                    now: datetime | None = None, ledger: RunLedger | None = None,
                    trigger_source: str = "archive") -> dict:
    if date.fromisoformat(business_day).isoformat() != business_day:
        raise ValueError("noncanonical business date")
    path = team / ".runs/locks" / f"closing_archive-{business_day}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"closing archive already running for {business_day}") from exc
        return _capture_closing(business_day, team=team, calendar_path=calendar_path,
                                now=now, ledger=ledger, trigger_source=trigger_source)


def restore_archive(team: Path, receipt_hash: str, target: Path) -> dict:
    """Restore price inputs, not account state; never replace an existing directory."""
    if target.exists() or target.is_symlink() or target.resolve().is_relative_to(team.resolve()):
        raise ValueError("restore destination must be a new directory outside the source team")
    source = team / "market_archive/receipts" / f"{_digest(receipt_hash)}.json"
    try:
        receipt = json.loads(source.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid archive receipt: {exc}") from exc
    if not isinstance(receipt, dict) or receipt.get("receipt_hash") != receipt_hash:
        raise ValueError("archive receipt hash mismatch")
    document = verify_receipt(team, receipt)
    data_path, calendar_path, receipt_path = _receipt_objects(team, receipt)
    objects = {path.relative_to(team): path.read_bytes() for path in (data_path, calendar_path, receipt_path)}
    # Recheck captured bytes before any destination write; publication marker is written last.
    if (validate_document(receipt["date"], json.loads(objects[data_path.relative_to(team)])) != document
            or hashlib.sha256(objects[calendar_path.relative_to(team)]).hexdigest() != receipt["calendar_hash"]
            or json.loads(objects[receipt_path.relative_to(team)]) != receipt):
        raise ValueError("archive changed during restore")
    target.mkdir(parents=True, exist_ok=False)
    for relative, content in objects.items():
        write_immutable(target / relative, content)
    write_immutable(target / "replay_data" / f"{receipt['date']}.json", _json_bytes(document))
    verify_receipt(target, receipt)
    result = {"status": "verified", "scope": "market_data_only", "receipt_hash": receipt_hash,
              "date": receipt["date"], "payload_hash": receipt["payload_hash"]}
    write_immutable(target / "restore.json", _json_bytes(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", type=Path, default=TEAM)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--business-date", required=True)
    capture.add_argument("--calendar", type=Path, default=DEFAULT_TRADING_CALENDAR)
    restore = commands.add_parser("restore")
    restore.add_argument("--receipt-hash", required=True)
    restore.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        result = capture_closing(args.business_date, team=args.team, calendar_path=args.calendar)
    else:
        result = restore_archive(args.team, args.receipt_hash, args.target)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
