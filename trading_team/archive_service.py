"""Periodic, model-free closing archive checks with durable gap tracking."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from trading_team.collect import DEFAULT_TRADING_CALENDAR, load_trading_days
from trading_team.execution_bars import SHANGHAI, TEAM
from trading_team.market_archive import capture_closing, completed_day, required_symbols, verify_receipt

SCHEMA = "archive_service.v1"
CHECK_SECONDS = 300
MAX_CAPTURES_PER_TICK = 3


def _now(value: datetime | None) -> datetime:
    value = value or datetime.now(SHANGHAI)
    return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)


def _stamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("service state timestamp requires timezone")
    return stamp


def _read_state(team: Path) -> dict | None:
    path = team / ".runs/archive-service.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise ValueError("schema mismatch")
        for key in ("receipts", "errors"):
            if not isinstance(value.get(key), dict):
                raise ValueError(f"{key} missing")
        start = value["tracking_start"]
        _stamp(value["initialized_at"])
        if start is not None and date.fromisoformat(start).isoformat() != start:
            raise ValueError("noncanonical tracking start")
        if start is None and value["receipts"]:
            raise ValueError("tracking start missing")
        for day in set(value["receipts"]) | set(value["errors"]):
            if date.fromisoformat(day).isoformat() != day or not start or day < start:
                raise ValueError("invalid tracked date")
        for error in value["errors"].values():
            if not isinstance(error, dict) or type(error.get("attempts")) is not int or error["attempts"] < 1:
                raise ValueError("invalid retry counter")
            _stamp(error["next_retry_at"])
        for key in ("last_tick_started_at", "last_tick_finished_at"):
            if value.get(key):
                _stamp(value[key])
        return value
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"invalid archive service state: {exc}") from exc


def _save_state(team: Path, state: dict) -> None:
    path = team / ".runs/archive-service.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _due(state: dict, calendar: Path, now: datetime) -> tuple[str, list[str]]:
    expected = completed_day(now.date().isoformat(), calendar, now)
    initialized = _stamp(state["initialized_at"]) if state.get("initialized_at") else now
    start = state.get("tracking_start") or completed_day(initialized.date().isoformat(), calendar, initialized)
    sessions = load_trading_days(calendar)
    if start not in sessions or start > expected:
        raise ValueError("calendar or clock no longer covers service tracking start")
    if any(day not in sessions or day > expected for day in state["receipts"]):
        raise ValueError("calendar or clock disagrees with previously archived sessions")
    return expected, sorted(day for day in sessions if start <= day <= expected)


def _verify_day(team: Path, day: str, receipt: dict, required: set[str]) -> None:
    document = verify_receipt(team, receipt)
    if document["date"] != day or not required <= document["bars"].keys():
        raise ValueError("archive date or symbol coverage mismatch")


def service_status(*, team: Path = TEAM, calendar_path: Path = DEFAULT_TRADING_CALENDAR,
                   now: datetime | None = None) -> dict:
    """Read and independently validate health; never repair files or query the network."""
    current = _now(now)
    result = {"schema": SCHEMA, "status": "uninitialized", "observed_at": current.isoformat(),
              "heartbeat": "missing", "expected_session": None, "latest_verified_session": None,
              "tracking_start": None, "last_tick_at": None, "missing_sessions": [],
              "invalid_sessions": [], "errors": {}, "verified_count": 0, "error": None}
    try:
        state = _read_state(team)
    except ValueError as exc:
        return {**result, "status": "invalid", "error": str(exc)}
    empty = {"receipts": {}, "errors": {}}
    try:
        expected, days = _due(state or empty, calendar_path, current)
        result["expected_session"] = expected
    except (OSError, ValueError, TypeError, EOFError) as exc:
        return {**result, "status": "failed", "error": f"calendar unavailable: {exc}"}
    if state is None:
        return {**result, "missing_sessions": days}
    result.update(tracking_start=state["tracking_start"], errors=dict(state["errors"]),
                  last_tick_at=state.get("last_tick_finished_at"), error=state.get("error"),
                  trigger_source=state.get("trigger_source"))
    if result["last_tick_at"]:
        age = (current - _stamp(result["last_tick_at"])).total_seconds()
        result["heartbeat"] = "fresh" if 0 <= age <= CHECK_SECONDS * 2 else "stale" if age > 0 else "future"
    try:
        required = set(required_symbols(team))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        return {**result, "status": "failed", "error": f"archive universe unavailable: {exc}"}
    verified = []
    for day in days:
        receipt = state["receipts"].get(day)
        if receipt is None:
            result["missing_sessions"].append(day)
            continue
        try:
            _verify_day(team, day, receipt, required if day == expected else set())
            verified.append(day)
        except ValueError as exc:
            result["invalid_sessions"].append(day)
            result["errors"].setdefault(day, {"error": str(exc)})
    result.update(latest_verified_session=max(verified, default=None), verified_count=len(verified))
    result["status"] = ("healthy" if result["heartbeat"] == "fresh" and not (
        result["missing_sessions"] or result["invalid_sessions"] or result["errors"] or result["error"])
        else "degraded")
    if state.get("status") == "running":
        result.update(status="degraded", error="last tick has no completion record")
    return result


def tick(*, team: Path = TEAM, calendar_path: Path = DEFAULT_TRADING_CALENDAR,
         now: datetime | None = None, trigger_source: str = "manual") -> dict:
    current = _now(now)
    path = team / ".runs/locks/archive-service.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("archive service already running") from exc
        state = _read_state(team) or {"schema": SCHEMA, "initialized_at": current.isoformat(),
                                     "tracking_start": None, "receipts": {}, "errors": {}}
        state.update(status="running", error=None, last_tick_started_at=current.isoformat(),
                     trigger_source=trigger_source)
        _save_state(team, state)
        captured = []
        try:
            try:
                expected, days = _due(state, calendar_path, current)
            except (OSError, ValueError, EOFError, TypeError) as exc:
                raise ValueError(f"calendar unavailable: {exc}") from exc
            state["tracking_start"] = state["tracking_start"] or days[0]
            required = set(required_symbols(team))
            attempts = 0
            # Capture the newest close first; unavailable historical quotes must not lose today's close.
            for day in reversed(days):
                receipt = state["receipts"].get(day)
                if receipt is not None:
                    try:
                        _verify_day(team, day, receipt, required if day == expected else set())
                        state["errors"].pop(day, None)
                        continue
                    except ValueError:
                        pass
                previous = state["errors"].get(day, {})
                if previous.get("next_retry_at") and current < _stamp(previous["next_retry_at"]):
                    continue
                if attempts >= MAX_CAPTURES_PER_TICK:
                    continue
                attempts += 1
                try:
                    receipt = capture_closing(day, team=team, calendar_path=calendar_path,
                                              now=current, trigger_source=trigger_source)
                    _verify_day(team, day, receipt, required if day == expected else set())
                    state["receipts"][day] = receipt
                    state["errors"].pop(day, None)
                    captured.append(day)
                except Exception as exc:
                    count = previous.get("attempts", 0) + 1
                    delay = min(3600, CHECK_SECONDS * 2 ** min(count - 1, 4))
                    state["errors"][day] = {"attempts": count, "error": f"{type(exc).__name__}: {exc}",
                                             "last_attempt_at": current.isoformat(),
                                             "next_retry_at": (current + timedelta(seconds=delay)).isoformat()}
                _save_state(team, state)
            state["status"] = "checked"
        except Exception as exc:
            state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        state["last_tick_finished_at"] = (_now(None) if now is None else current).isoformat()
        _save_state(team, state)
        result = service_status(team=team, calendar_path=calendar_path,
                                now=_now(None) if now is None else current)
        return {**result, "captured": captured}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("tick", "status"))
    parser.add_argument("--team", type=Path, default=TEAM)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_TRADING_CALENDAR)
    parser.add_argument("--trigger-source", choices=("manual", "launchd"), default="manual")
    args = parser.parse_args()
    try:
        kwargs = {"team": args.team, "calendar_path": args.calendar}
        result = (tick(**kwargs, trigger_source=args.trigger_source) if args.command == "tick"
                  else service_status(**kwargs))
    except (OSError, ValueError, RuntimeError) as exc:
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False))
    if args.command == "tick" and result["status"] != "healthy":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
