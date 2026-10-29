import json

import pytest

from trading_team import notification_outbox as outbox
from trading_team.notification_outbox import push_once


def test_notification_is_sent_once_per_plan_version(tmp_path):
    team = tmp_path / "trading_team"
    calls = []

    def sender(day):
        calls.append(day)
        return {"pushed": True, "date": day}

    first = push_once("2026-09-21", "plan-v1", team=team, sender=sender)
    second = push_once("2026-09-21", "plan-v1", team=team, sender=sender)
    third = push_once("2026-09-21", "plan-v2", team=team, sender=sender)

    assert first["status"] == "sent"
    assert second["status"] == "skipped"
    assert third["status"] == "sent"
    assert calls == ["2026-09-21", "2026-09-21"]


def test_false_result_is_unknown_and_cannot_be_automatically_retried(tmp_path):
    team = tmp_path / "trading_team"

    try:
        push_once(
            "2026-09-21",
            "plan-v1",
            team=team,
            sender=lambda day: {"pushed": False, "date": day},
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed notification must raise")

    with pytest.raises(RuntimeError, match="unknown"):
        push_once("2026-09-21", "plan-v1", team=team,
                  sender=lambda _: pytest.fail("ambiguous result must not resend"))
    assert outbox.status("2026-09-21", "plan-v1", team=team)["status"] == "unknown"


def test_intent_is_durable_before_sender_is_called(tmp_path):
    team = tmp_path / "trading_team"
    def sender(day):
        item = outbox.status(day, "plan-v1", team=team)
        assert item["status"] == "unknown"
        assert item["attempt_count"] == 1
        return {"pushed": True, "message_id": "om_test"}
    push_once("2026-09-21", "plan-v1", team=team, sender=sender)
    item = outbox.status("2026-09-21", "plan-v1", team=team)
    assert item["status"] == "sent" and item["message_id"] == "om_test"


def test_remote_success_then_receipt_failure_never_resends(tmp_path, monkeypatch):
    team = tmp_path / "trading_team"
    original = outbox.atomic_write_json
    sent = []
    def write(path, doc):
        if doc.get("status") == "sent":
            raise OSError("disk unavailable after delivery")
        original(path, doc)
    monkeypatch.setattr(outbox, "atomic_write_json", write)
    with pytest.raises(RuntimeError, match="unknown"):
        push_once("2026-09-21", "plan-v1", team=team,
                  sender=lambda day: sent.append(day) or {"pushed": True, "message_id": "om_one"})
    monkeypatch.setattr(outbox, "atomic_write_json", original)
    with pytest.raises(RuntimeError, match="unknown"):
        push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: sent.append("duplicate"))
    assert sent == ["2026-09-21"]


def test_failure_before_intent_commit_does_not_call_sender(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "atomic_write_json", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        push_once("2026-09-21", "plan-v1", team=tmp_path / "trading_team",
                  sender=lambda _: pytest.fail("no durable intent"))


def test_preflight_not_sent_is_retryable_and_preserves_attempt(tmp_path):
    team = tmp_path / "trading_team"
    def preflight(_):
        raise outbox.NotSent("credentials unavailable before message request")
    with pytest.raises(outbox.NotSent):
        push_once("2026-09-21", "plan-v1", team=team, sender=preflight)
    assert outbox.status("2026-09-21", "plan-v1", team=team)["status"] == "retryable"
    push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: {"pushed": True})
    assert outbox.status("2026-09-21", "plan-v1", team=team)["attempt_count"] == 2


@pytest.mark.parametrize("outcome", ["delivered", "not_delivered"])
def test_manual_resolution_is_version_bound_idempotent_and_never_sends(tmp_path, outcome):
    team = tmp_path / "trading_team"
    with pytest.raises(RuntimeError):
        push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: {"pushed": False})
    state = outbox.status("2026-09-21", "plan-v1", team=team)
    kwargs = dict(team=team, expected_hash=state["record_hash"], attempt_id=state["attempt_id"],
                  outcome=outcome, operator="offline-checker", evidence="fixture transport log receipt",
                  message_id="om_manual" if outcome == "delivered" else None)
    first = outbox.resolve("2026-09-21", "plan-v1", **kwargs)
    path = team / ".runs/outbox/feishu/2026-09-21-plan-v1.json"
    before = path.read_bytes()
    assert outbox.resolve("2026-09-21", "plan-v1", **kwargs) == first
    assert path.read_bytes() == before
    assert first["status"] == ("sent" if outcome == "delivered" else "retryable")
    calls = []
    push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: calls.append(1) or {"pushed": True})
    assert len(calls) == (0 if outcome == "delivered" else 1)
    with pytest.raises(ValueError):
        outbox.resolve("2026-09-21", "plan-v1", **(kwargs | {"evidence": "changed claim"}))


@pytest.mark.parametrize("field,value", [("expected_hash", "wrong"), ("attempt_id", "wrong"),
                                          ("operator", ""), ("evidence", "")])
def test_resolution_rejects_stale_identity_or_missing_evidence(tmp_path, field, value):
    team = tmp_path / "trading_team"
    with pytest.raises(RuntimeError):
        push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: {"pushed": False})
    state = outbox.status("2026-09-21", "plan-v1", team=team)
    kwargs = dict(team=team, expected_hash=state["record_hash"], attempt_id=state["attempt_id"],
                  outcome="not_delivered", operator="checker", evidence="transport log")
    kwargs[field] = value
    with pytest.raises(ValueError):
        outbox.resolve("2026-09-21", "plan-v1", **kwargs)
    assert outbox.status("2026-09-21", "plan-v1", team=team)["record_hash"] == state["record_hash"]


def test_changed_content_or_recipient_cannot_reuse_a_bound_notification(tmp_path):
    team = tmp_path / "trading_team"
    with pytest.raises(RuntimeError):
        push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: {"pushed": False},
                  fingerprint="content-recipient-v1")
    with pytest.raises(ValueError, match="fingerprint"):
        push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: pytest.fail("send"),
                  fingerprint="content-recipient-v2")


@pytest.mark.parametrize("content", ["{broken", "[]", '{"status":"unexpected"}'])
def test_corrupt_existing_record_is_never_overwritten_or_sent(tmp_path, content):
    team = tmp_path / "trading_team"
    path = team / ".runs/outbox/feishu/2026-09-21-plan-v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(ValueError):
        push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: pytest.fail("send"))
    assert path.read_text() == content
    assert outbox.status("2026-09-21", "plan-v1", team=team)["status"] == "invalid"


def test_status_and_summary_are_read_only_and_keep_old_unknown_visible(tmp_path):
    team = tmp_path / "trading_team"
    assert outbox.summary(team=team)["status"] == "missing"
    assert not team.exists()
    with pytest.raises(RuntimeError):
        push_once("2026-09-18", "old", team=team, sender=lambda _: {"pushed": False})
    push_once("2026-09-21", "new", team=team, sender=lambda _: {"pushed": True})
    files = {str(p): p.read_bytes() for p in (team / ".runs/outbox").rglob("*.json")}
    result = outbox.summary(team=team)
    assert result["status"] == "attention"
    assert result["counts"]["unknown"] == 1 and result["counts"]["sent"] == 1
    assert result["issues"][0]["date"] == "2026-09-18"
    assert files == {str(p): p.read_bytes() for p in (team / ".runs/outbox").rglob("*.json")}


def test_sent_version_is_not_resent_when_approval_text_changes(tmp_path):
    team = tmp_path / "trading_team"
    push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: {"pushed": True}, fingerprint="before")
    result = push_once("2026-09-21", "plan-v1", team=team,
                       sender=lambda _: pytest.fail("already sent"), fingerprint="after")
    assert result["status"] == "skipped"
    assert result["receipt"]["fingerprint"] == "before"


@pytest.mark.parametrize("field", ["day", "plan_hash", "channel"])
def test_path_traversal_is_rejected_before_sender(tmp_path, field):
    args = dict(day="2026-09-21", plan_hash="plan-v1", channel="feishu", team=tmp_path / "trading_team",
                sender=lambda _: pytest.fail("unsafe path"))
    args[field] = "../outside"
    with pytest.raises(ValueError):
        push_once(**args)


def test_corrupt_attempt_history_is_visible_and_not_retried(tmp_path):
    team = tmp_path / "trading_team"
    push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: {"pushed": True})
    path = team / ".runs/outbox/feishu/2026-09-21-plan-v1.json"
    doc = json.loads(path.read_text())
    doc["attempts"][0]["result"]["pushed"] = False
    path.write_text(json.dumps(doc))
    assert outbox.status("2026-09-21", "plan-v1", team=team)["status"] == "invalid"
    with pytest.raises(ValueError):
        push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: pytest.fail("corrupt"))


def test_known_provider_ack_cannot_be_reconciled_as_not_delivered(tmp_path, monkeypatch):
    team = tmp_path / "trading_team"
    original = outbox.atomic_write_json
    def write(path, doc):
        if doc.get("status") == "sent":
            raise OSError("ack commit failed")
        original(path, doc)
    monkeypatch.setattr(outbox, "atomic_write_json", write)
    with pytest.raises(outbox.DeliveryUnknown):
        push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: {"pushed":True,"message_id":"om_known"})
    item = outbox.status("2026-09-21", "plan-v1", team=team)
    kwargs = dict(team=team, expected_hash=item["record_hash"], attempt_id=item["attempt_id"],
                  operator="checker", evidence="local observation")
    with pytest.raises(ValueError, match="acknowledgement"):
        outbox.resolve("2026-09-21", "plan-v1", outcome="not_delivered", **kwargs)
    with pytest.raises(ValueError, match="acknowledgement"):
        outbox.resolve("2026-09-21", "plan-v1", outcome="delivered", message_id="om_other", **kwargs)


@pytest.mark.parametrize("field,value", [("started_at", None), ("result", []), ("status", "bogus")])
def test_malformed_attempt_is_invalid_even_with_recomputed_checksum(tmp_path, field, value):
    team = tmp_path / "trading_team"
    push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: {"pushed":True})
    path = team / ".runs/outbox/feishu/2026-09-21-plan-v1.json"
    doc = json.loads(path.read_text())
    doc["attempts"][0][field] = value
    outbox._write(path, doc)
    assert outbox.status("2026-09-21", "plan-v1", team=team)["status"] == "invalid"
    with pytest.raises(ValueError):
        push_once("2026-09-21", "plan-v1", team=team, sender=lambda _: pytest.fail("malformed"))


def test_symlink_outbox_is_not_read_as_a_healthy_empty_history(tmp_path):
    team = tmp_path / "trading_team"
    (team / ".runs").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (team / ".runs/outbox").symlink_to(external, target_is_directory=True)
    assert outbox.summary(team=team)["status"] == "attention"
    assert outbox.summary(team=team)["counts"]["invalid"] == 1
