"""Exact-session, unadjusted execution inputs with immutable source evidence."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import uuid
from dataclasses import asdict
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import h5py
import numpy as np

from paper.execution_model import Bar
from trading_team.collect import DEFAULT_TRADING_CALENDAR, fetch_snapshot_tx, load_trading_days

TEAM = Path(__file__).resolve().parent
BUNDLE = Path.home() / ".rqalpha" / "bundle"
SCHEMA = "execution_bars.v2"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _number(row: dict, field: str, *, zero: bool = False) -> float:
    value = row.get(field)
    if isinstance(value, bool):
        raise ValueError(f"{field}: boolean is not a price or volume")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field}: numeric value missing") from exc
    if not math.isfinite(result) or result < 0 or (result == 0 and not zero):
        raise ValueError(f"{field}: invalid numeric value")
    return result


def _bar_from_row(code: str, row: dict, day: str | None = None) -> Bar:
    if not isinstance(row, dict) or row.get("price_basis") != "unadjusted":
        raise ValueError(f"{code}: execution requires unadjusted prices")
    row_day = row.get("date")
    if not isinstance(row_day, str):
        raise ValueError(f"{code}: quote date missing")
    date.fromisoformat(row_day)
    if day is not None and row_day != day:
        raise ValueError(f"{code}: quote date does not match {day}")
    suspended = row.get("suspended")
    if not isinstance(suspended, bool):
        raise ValueError(f"{code}: explicit suspension state missing")
    volume = _number(row, "volume", zero=True)
    if volume == 0 and not suspended:
        raise ValueError(f"{code}: zero volume cannot be executable")
    values = {key: _number(row, key) for key in ("close", "prev_close")}
    for key in ("open", "high", "low"):
        values[key] = None if suspended and row.get(key) is None else _number(row, key)
    if not suspended and not (
        values["low"] <= min(values["open"], values["close"])
        <= max(values["open"], values["close"]) <= values["high"]
    ):
        raise ValueError(f"{code}: inconsistent OHLC")
    up = down = None
    if code != "000300" and not suspended:
        up, down = _number(row, "limit_up"), _number(row, "limit_down")
        if not down < up or values["low"] < down - 1e-6 or values["high"] > up + 1e-6:
            raise ValueError(f"{code}: OHLC outside supplied daily limits")
    elif code != "000300" and row.get("limit_up") is not None and row.get("limit_down") is not None:
        up, down = _number(row, "limit_up"), _number(row, "limit_down")
    return Bar(**values, suspended=suspended, volume=volume, limit_up=up, limit_down=down)


def seal_document(day: str, rows: dict, sources: dict) -> dict:
    for code, row in rows.items():
        bar = _bar_from_row(code, row, day)
        source = sources.get(code)
        if not isinstance(source, dict) or not isinstance(source.get("record"), dict):
            raise ValueError(f"{code}: source evidence missing")
        raw = source["record"]
        if source.get("source_hash") != _hash(raw):
            raise ValueError(f"{code}: source record hash mismatch")
        if source.get("kind") == "rqalpha_bundle":
            if raw.get("datetime") != int(day.replace("-", "")) * 1000000:
                raise ValueError(f"{code}: bundle record date mismatch")
            origin = {**raw, "date": day, "price_basis": "unadjusted", "suspended": raw.get("volume") == 0}
        elif source.get("kind") in ("context_snapshot", "tencent_closing_snapshot"):
            origin = _snapshot_row(code, raw, day)
        else:
            raise ValueError(f"{code}: unsupported execution source")
        if asdict(bar) != asdict(_bar_from_row(code, origin, day)):
            raise ValueError(f"{code}: execution row does not match source record")
    document = {"schema": SCHEMA, "date": day, "price_basis": "unadjusted",
                "bars": rows, "sources": sources}
    return {**document, "payload_hash": _hash(document)}


def validate_document(day: str, document: dict) -> dict:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError("legacy replay cache requires audited replacement; schema mismatch")
    if document.get("date") != day or document.get("price_basis") != "unadjusted":
        raise ValueError("replay cache date or price basis mismatch")
    expected = document.get("payload_hash")
    content = {key: value for key, value in document.items() if key != "payload_hash"}
    if expected != _hash(content):
        raise ValueError("replay cache payload hash mismatch")
    if not isinstance(document.get("bars"), dict) or not isinstance(document.get("sources"), dict):
        raise ValueError("replay cache bars or sources missing")
    seal_document(day, document["bars"], document["sources"])
    return document


def _cache_document(day: str, team: Path) -> dict | None:
    path = team / "replay_data" / f"{day}.json"
    if not path.exists():
        return None
    return validate_document(day, json.loads(path.read_text(encoding="utf-8")))


def write_immutable(path: Path, content: bytes) -> None:
    """Publish a complete content-addressed object without replacing prior evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise ValueError(f"archive object changed: {path.name}")
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def archive_document(day: str, team: Path, document: dict) -> Path:
    validate_document(day, document)
    path = team / "market_archive" / day / f"{document['payload_hash']}.json"
    write_immutable(path, json.dumps(document, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False, indent=2).encode("utf-8"))
    return path


def _cached_bars(day: str, team: Path) -> dict[str, Bar] | None:
    document = _cache_document(day, team)
    return None if document is None else {
        code: _bar_from_row(code, row, day) for code, row in document["bars"].items()
    }


def _snapshot_row(code: str, item: dict, day: str) -> dict:
    stamp = datetime.strptime(str(item.get("quote_time") or ""), "%Y%m%d%H%M%S")
    if stamp.date().isoformat() != day or stamp.time() < time(15):
        raise ValueError(f"{code}: snapshot is not {day} closing data")
    volume = _number(item, "volume", zero=True) * 100
    row = {**item, "date": day, "volume": volume, "suspended": volume == 0}
    if volume == 0:
        for field in ("open", "high", "low"):
            if row.get(field) == 0:
                row[field] = None
    _bar_from_row(code, row, day)
    return row


def _context_rows(day: str, codes: set[str], team: Path, *, max_context_day: str | None = None) -> tuple[dict, dict]:
    rows, sources = {}, {}
    for path in sorted((team / "context").glob("*/manifest.json"), reverse=True):
        if max_context_day and path.parent.name > max_context_day:
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest.get("ok") is not True or manifest.get("latest_trade_date") != day:
                continue
            snapshot_path = path.with_name("snapshot.json")
            raw = snapshot_path.read_bytes()
            snapshot = json.loads(raw)
            for code in sorted(codes - rows.keys()):
                if code not in snapshot:
                    continue
                try:
                    rows[code] = _snapshot_row(code, snapshot[code], day)
                    sources[code] = {"kind": "context_snapshot", "path": str(snapshot_path.relative_to(team)),
                                     "source_file_hash": hashlib.sha256(raw).hexdigest(),
                                     "record": snapshot[code], "source_hash": _hash(snapshot[code]),
                                     "suspension_evidence": "zero_volume_conservative"}
                except (ValueError, TypeError):
                    continue
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return rows, sources


def load_closing_marks(team: Path, as_of_day: str | None = None, *,
                       max_quote_day: str | None = None, calendar_path: Path | None = None,
                       now: datetime | None = None) -> dict:
    """Read archived raw prices only; never fetch, repair, or create state for a view."""
    empty = {"source_date": None, "quote_asof": None, "prices": {}, "bars": {}, "evidence": {},
             "source": None, "price_basis": None}
    current = now or datetime.now(SHANGHAI)
    current = current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    today = current.date().isoformat()
    as_of_day = min(as_of_day or today, today)
    date.fromisoformat(as_of_day)
    try:
        sessions = load_trading_days(calendar_path or DEFAULT_TRADING_CALENDAR)
        if not sessions or not min(sessions) <= as_of_day <= max(sessions):
            raise ValueError(f"calendar does not cover {as_of_day}")
        allowed = {day for day in sessions if day <= as_of_day and (
            not max_quote_day or day <= max_quote_day) and (
            day < today or current.time() >= time(15, 10))}
    except (OSError, ValueError, EOFError, TypeError) as exc:
        return {**empty, "error": f"valuation calendar unavailable: {exc}"}
    candidates = {path.stem for path in (team / "replay_data").glob("????-??-??.json")
                  if path.stem in allowed}
    context_codes: dict[str, set[str]] = {}
    for manifest_path in (team / "context").glob("????-??-??/manifest.json"):
        if manifest_path.parent.name > as_of_day:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            day = manifest.get("latest_trade_date")
            if manifest.get("ok") is not True or day not in allowed:
                continue
            snapshot = json.loads(manifest_path.with_name("snapshot.json").read_text(encoding="utf-8"))
            if isinstance(snapshot, dict):
                context_codes.setdefault(day, set()).update(snapshot)
                candidates.add(day)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    for day in sorted(candidates, reverse=True):
        try:
            cached = _cache_document(day, team)
        except (OSError, ValueError, TypeError) as exc:
            return {**empty, "rejected_quote_day": day, "error": f"invalid execution cache: {exc}"}
        rows = dict(cached["bars"]) if cached else {}
        sources = dict(cached["sources"]) if cached else {}
        additional, origins = _context_rows(day, context_codes.get(day, set()) - rows.keys(),
                                             team, max_context_day=as_of_day)
        rows.update(additional)
        sources.update(origins)
        if rows:
            collected = [source["path"].split("/")[1] for source in origins.values()]
            bars = {code: _bar_from_row(code, row, day) for code, row in rows.items()}
            return {**empty, "source_date": max(collected) if collected else day,
                    "quote_asof": day, "source": "validated_unadjusted_close.v1", "price_basis": "unadjusted",
                    "prices": {code: bar.close for code, bar in bars.items()},
                    "bars": {code: asdict(bar) for code, bar in bars.items()},
                    "evidence": {code: {key: source[key] for key in ("kind", "source_hash", "record")}
                                 for code, source in sources.items()}}
    return {**empty, "error": "no validated unadjusted closing prices"}


def _bundle_rows(day: str, codes: set[str], bundle: Path) -> tuple[dict, dict]:
    rows, sources = {}, {}
    stamp = int(day.replace("-", "")) * 1000000
    for code in sorted(codes):
        filename = "indexes.h5" if code == "000300" else "stocks.h5"
        symbol = code + (".XSHG" if code.startswith("6") or code == "000300" else ".XSHE")
        try:
            with h5py.File(bundle / filename, "r") as handle:
                if symbol not in handle:
                    continue
                dataset = handle[symbol]
                indices = np.flatnonzero(dataset["datetime"] == stamp)
                if len(indices) != 1:
                    continue
                record = dataset[int(indices[0])]
                raw = {key: float(record[key]) for key in record.dtype.names}
            row = {**raw, "date": day, "price_basis": "unadjusted", "suspended": raw["volume"] == 0}
            _bar_from_row(code, row, day)
            rows[code] = row
            sources[code] = {"kind": "rqalpha_bundle", "file": filename, "symbol": symbol,
                             "record": raw, "source_hash": _hash(raw),
                             "suspension_evidence": "zero_volume_conservative"}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return rows, sources


def load_execution_bars(day: str, codes: list[str], team: Path = TEAM, *,
                        calendar_path: Path | None = None, bundle: Path = BUNDLE,
                        now: datetime | None = None) -> dict[str, Bar]:
    requested = date.fromisoformat(day)
    current = now or datetime.now(SHANGHAI)
    current = current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    sessions = load_trading_days(calendar_path or DEFAULT_TRADING_CALENDAR)
    if day not in sessions or requested > current.date() or (
        requested == current.date() and current.time() < time(15, 10)
    ):
        raise ValueError(f"{day}: execution session not completed")
    wanted = set(codes) | {"000300"}
    if any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in wanted):
        raise ValueError("invalid A-share symbol")
    lock_path = team / ".runs/locks" / f"replay-{day}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = _cache_document(day, team)
        rows = dict(existing["bars"]) if existing else {}
        sources = dict(existing["sources"]) if existing else {}
        for getter in (
            lambda missing: _context_rows(day, missing, team),
            lambda missing: _bundle_rows(day, missing, bundle),
        ):
            missing = wanted - rows.keys()
            if not missing:
                break
            additions, origins = getter(missing)
            rows.update(additions)
            sources.update(origins)
        missing = wanted - rows.keys()
        if missing:
            secids = [("1." if code.startswith("6") or code == "000300" else "0.") + code
                      for code in sorted(missing)]
            snapshot = fetch_snapshot_tx(secids)
            for code in sorted(missing):
                if code not in snapshot:
                    raise ValueError(f"{code}: complete unadjusted execution evidence missing for {day}")
                rows[code] = _snapshot_row(code, snapshot[code], day)
                sources[code] = {"kind": "tencent_closing_snapshot", "source_hash": _hash(snapshot[code]),
                                 "record": snapshot[code],
                                 "observed_at": current.isoformat(),
                                 "suspension_evidence": "zero_volume_conservative"}
        document = seal_document(day, rows, sources)
        archive_document(day, team, document)
        if document != existing:
            path = team / "replay_data" / f"{day}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False),
                                     encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return {code: _bar_from_row(code, rows[code], day) for code in sorted(wanted)}
