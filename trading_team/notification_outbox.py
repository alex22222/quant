# -*- coding: utf-8 -*-
"""Durable send intent; ambiguous delivery requires explicit reconciliation."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path
import re
from typing import Callable
import uuid

from trading_team.state_lock import atomic_write_json, state_lock

SCHEMA = "notification_outbox.v2"
TEAM = Path(__file__).resolve().parent


class NotSent(RuntimeError):
    """Only for a failure proven to occur before the message request."""


class DeliveryUnknown(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def notification_id(day: str, plan_hash: str, channel: str = "feishu") -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([channel, day, plan_hash])))


def _path(team: Path, day: str, plan_hash: str, channel: str) -> Path:
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
           for value in (day, plan_hash, channel)):
        raise ValueError("invalid notification identity")
    path = team / ".runs/outbox" / channel / f"{day}-{plan_hash}.json"
    if any(p.is_symlink() for p in (team, team / ".runs", path.parent.parent, path.parent, path)):
        raise ValueError("notification symlink refused")
    return path


def _digest(doc: dict) -> str:
    return hashlib.sha256(json.dumps({k: v for k, v in doc.items() if k != "content_hash"},
                                    ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write(path: Path, doc: dict) -> None:
    doc["content_hash"] = _digest(doc)
    atomic_write_json(path, doc)


def _attempt_state(attempt: dict) -> str:
    resolution = attempt.get("resolution")
    if resolution:
        return "sent" if resolution["outcome"] == "delivered" else "retryable"
    return "unknown" if attempt["status"] == "sending" else attempt["status"]


def _read(path: Path, day: str, plan_hash: str, channel: str, raw: bytes | None = None) -> dict:
    doc = json.loads(path.read_bytes() if raw is None else raw)
    if not isinstance(doc, dict):
        raise ValueError("notification record must be an object")
    if any(doc.get(k, v) != v for k, v in (("date", day), ("plan_hash", plan_hash), ("channel", channel))):
        raise ValueError("notification identity mismatch")
    if "schema" not in doc and doc.get("status") == "sent":
        return doc  # Legacy receipts suppress repeats, but are not upgraded to provider evidence.
    if (doc.get("schema") != SCHEMA or doc.get("content_hash") != _digest(doc)
            or any(doc.get(k) != v for k, v in (("date", day), ("plan_hash", plan_hash), ("channel", channel)))
            or doc.get("notification_id") != notification_id(day, plan_hash, channel)
            or not isinstance(doc.get("attempts"), list) or not doc["attempts"]):
        raise ValueError("notification record invalid")
    ids = set()
    for attempt in doc["attempts"]:
        if (not isinstance(attempt, dict) or not isinstance(attempt.get("attempt_id"), str)
                or attempt["attempt_id"] in ids or not attempt["attempt_id"]
                or not isinstance(attempt.get("started_at"), str) or not attempt["started_at"]
                or not isinstance(attempt.get("result", {}), dict)
                or attempt.get("status") not in ("sending", "unknown", "retryable", "sent")):
            raise ValueError("notification attempt invalid")
        if attempt["status"] == "sent" and attempt.get("result", {}).get("pushed") is not True:
            raise ValueError("notification acknowledgement missing")
        ids.add(attempt["attempt_id"])
        resolution = attempt.get("resolution")
        if resolution is not None and (not isinstance(resolution, dict)
                or resolution.get("outcome") not in ("delivered", "not_delivered")
                or not all(isinstance(resolution.get(k), str) and resolution[k].strip()
                           for k in ("operator", "evidence", "expected_hash", "resolved_at"))):
            raise ValueError("notification resolution invalid")
    state = _attempt_state(doc["attempts"][-1])
    if ("unknown" if doc.get("status") == "sending" else doc.get("status")) != state:
        raise ValueError("notification state disagrees with attempts")
    if any(_attempt_state(a) != "retryable" for a in doc["attempts"][:-1]):
        raise ValueError("notification retried without non-delivery evidence")
    return doc


def status(day: str, plan_hash: str, *, team: Path = TEAM, channel: str = "feishu") -> dict:
    path = _path(team, day, plan_hash, channel)
    identity = {"date": day, "plan_hash": plan_hash, "channel": channel}
    if not path.exists():
        return identity | {"status": "missing", "attempt_count": 0}
    try:
        raw = path.read_bytes()
        doc = _read(path, day, plan_hash, channel, raw)
        digest = hashlib.sha256(raw).hexdigest()
        if doc.get("schema") != SCHEMA:
            return identity | {"status": "legacy_sent", "evidence": "legacy_unverified", "record_hash": digest}
        attempt = doc["attempts"][-1]
        resolution = attempt.get("resolution") or {}
        message_id = resolution.get("message_id") or attempt.get("result", {}).get("message_id")
        return identity | {"status": _attempt_state(attempt), "record_hash": digest,
                           "notification_id": doc["notification_id"], "attempt_id": attempt["attempt_id"],
                           "attempt_count": len(doc["attempts"]), "started_at": attempt["started_at"],
                           "attempts": doc["attempts"],
                           "message_id": message_id, "resolution": resolution or None,
                           "evidence": "manual_attestation" if resolution else
                           "provider_acknowledged" if message_id and attempt["status"] == "sent" else
                           "sender_acknowledged" if attempt["status"] == "sent" else "unconfirmed"}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return identity | {"status": "invalid", "error": str(exc), "attempt_count": None}


def summary(*, team: Path = TEAM) -> dict:
    root = team / ".runs/outbox"
    items = []
    unsafe = any(p.is_symlink() for p in (team, team / ".runs", root))
    if unsafe or (root.exists() and not root.is_dir()):
        items.append({"status": "invalid", "path": ".runs/outbox", "error": "notification directory invalid"})
    for path in [] if unsafe else sorted(root.glob("*/*.json")):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("notification symlink refused")
            doc = json.loads(path.read_text(encoding="utf-8"))
            item = status(doc["date"], doc["plan_hash"], team=team, channel=path.parent.name)
            if path != _path(team, doc["date"], doc["plan_hash"], path.parent.name):
                raise ValueError("notification filename mismatch")
        except (OSError, ValueError, TypeError, KeyError):
            item = {"status": "invalid", "path": str(path.relative_to(team)), "error": "invalid notification record"}
        items.append(item)
    counts = {key: sum(item["status"] == key for item in items)
              for key in ("sent", "legacy_sent", "unknown", "retryable", "invalid")}
    issues = [item for item in items if item["status"] in ("unknown", "retryable", "invalid")]
    return {"status": "attention" if issues else "available" if items else "missing",
            "counts": counts, "issues": issues, "items": items}


def push_once(
    day: str,
    plan_hash: str,
    *,
    team: Path,
    sender: Callable[[str], dict],
    channel: str = "feishu",
    fingerprint: str | None = None,
) -> dict:
    with state_lock(team):
        return _push_once(day, plan_hash, team=team, sender=sender, channel=channel, fingerprint=fingerprint)


def _push_once(day: str, plan_hash: str, *, team: Path,
               sender: Callable[[str], dict], channel: str, fingerprint: str | None) -> dict:
    marker = _path(team, day, plan_hash, channel)
    if marker.exists():
        doc = _read(marker, day, plan_hash, channel)
        if doc["status"] == "sent":
            return {"status": "skipped", "reason": "already sent", "receipt": doc}
        if doc.get("fingerprint") != fingerprint:
            raise ValueError("notification fingerprint changed; reconcile content and recipient")
        if doc["status"] != "retryable":
            raise DeliveryUnknown("notification delivery unknown; inspect and reconcile before retry")
    else:
        doc = {"schema": SCHEMA, "channel": channel, "date": day, "plan_hash": plan_hash,
               "notification_id": notification_id(day, plan_hash, channel), "fingerprint": fingerprint,
               "attempts": []}
    attempt = {"attempt_id": uuid.uuid4().hex, "started_at": _now(), "status": "sending"}
    doc["attempts"].append(attempt)
    doc["status"] = "sending"
    _write(marker, doc)
    try:
        result = sender(day)
        if not isinstance(result, dict) or result.get("pushed") is not True:
            raise DeliveryUnknown("sender did not return a delivery acknowledgement")
        attempt.update(status="sent", finished_at=_now(), result={"pushed": True})
        if isinstance(result.get("message_id"), str) and result["message_id"]:
            attempt["result"]["message_id"] = result["message_id"]
        doc["status"] = "sent"
        _write(marker, doc)
        return {"status": "sent", "receipt": doc}
    except Exception as exc:
        attempt.update(status="retryable" if isinstance(exc, NotSent) else "unknown",
                       finished_at=_now(), error_type=type(exc).__name__)
        doc["status"] = attempt["status"]
        try:
            _write(marker, doc)
        except OSError:
            # The durable pre-send intent still prevents a blind retry after persistence failure.
            pass
        if isinstance(exc, NotSent):
            raise
        raise DeliveryUnknown("notification delivery unknown; inspect and reconcile before retry") from exc


def resolve(day: str, plan_hash: str, *, team: Path = TEAM, channel: str = "feishu",
            expected_hash: str, attempt_id: str, outcome: str, operator: str,
            evidence: str, message_id: str | None = None) -> dict:
    if (outcome not in ("delivered", "not_delivered") or
            not all(isinstance(v, str) and v.strip() for v in (operator, evidence, expected_hash, attempt_id))
            or (message_id is not None and (not isinstance(message_id, str) or not message_id.strip()))
            or (outcome == "not_delivered" and message_id is not None)):
        raise ValueError("resolution requires outcome, operator, evidence and exact record identity")
    with state_lock(team):
        path = _path(team, day, plan_hash, channel)
        doc = _read(path, day, plan_hash, channel)
        if doc.get("schema") != SCHEMA:
            raise ValueError("legacy receipt cannot be recertified")
        attempt = doc["attempts"][-1]
        if attempt["attempt_id"] != attempt_id:
            raise ValueError("notification attempt changed")
        requested = dict(outcome=outcome, operator=operator, evidence=evidence,
                         message_id=message_id, expected_hash=expected_hash)
        previous = attempt.get("resolution")
        if previous:
            if any(previous.get(k) != v for k, v in requested.items()):
                raise ValueError("notification already reconciled with different evidence")
            return status(day, plan_hash, team=team, channel=channel)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError("notification record changed")
        if _attempt_state(attempt) != "unknown":
            raise ValueError("only unknown delivery can be manually reconciled")
        acknowledged_id = attempt.get("result", {}).get("message_id")
        if acknowledged_id and (outcome != "delivered" or (message_id and message_id != acknowledged_id)):
            raise ValueError("resolution conflicts with known provider acknowledgement")
        attempt["resolution"] = requested | {"resolved_at": _now()}
        doc["status"] = _attempt_state(attempt)
        _write(path, doc)
        return status(day, plan_hash, team=team, channel=channel)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Inspect or reconcile notifications; never send")
    parser.add_argument("command", choices=("status", "resolve"))
    parser.add_argument("--team", type=Path, default=TEAM)
    parser.add_argument("--date")
    parser.add_argument("--plan-hash")
    parser.add_argument("--channel", default="feishu")
    parser.add_argument("--expected-hash")
    parser.add_argument("--attempt-id")
    parser.add_argument("--outcome", choices=("delivered", "not_delivered"))
    parser.add_argument("--operator")
    parser.add_argument("--evidence")
    parser.add_argument("--message-id")
    args = parser.parse_args()
    if args.command == "resolve":
        result = resolve(args.date, args.plan_hash, team=args.team, channel=args.channel,
                         expected_hash=args.expected_hash, attempt_id=args.attempt_id, outcome=args.outcome,
                         operator=args.operator, evidence=args.evidence, message_id=args.message_id)
    elif args.date and args.plan_hash:
        result = status(args.date, args.plan_hash, team=args.team, channel=args.channel)
    elif args.date or args.plan_hash:
        parser.error("--date and --plan-hash must be supplied together")
    else:
        result = summary(team=args.team)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] in ("unknown", "invalid", "attention", "retryable") else 0


if __name__ == "__main__":
    raise SystemExit(main())
