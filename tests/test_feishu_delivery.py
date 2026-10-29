import importlib
import json
from pathlib import Path

import pytest
import requests

from trading_team import notification_outbox as outbox

DAY = "2026-09-21"
PLAN_HASH = "a" * 64


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path.name == "config.json":
            pytest.fail("credential file read during import")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    module = importlib.import_module("trading_team.push_feishu")
    team = tmp_path / "trading_team"
    monkeypatch.setattr(module, "_config", lambda: {"app_id":"fake-app", "app_secret":"fake-secret",
                                                  "receive_id":"fake-recipient", "receive_id_type":"chat_id"})
    monkeypatch.setattr(module, "_verify_source", lambda day, team: PLAN_HASH)
    monkeypatch.setattr(module, "build_card", lambda day, team=None: {"elements":[],"header":{"title":"fixture"}})
    monkeypatch.setattr(module, "get_feishu_token", lambda *args: "fake-token")
    return module, team


def test_direct_push_uses_outbox_and_provider_message_id(delivery, monkeypatch):
    module, team = delivery
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {"code":0,"data":{"message_id":"om_fixture"}}
    def post(url, **kwargs):
        assert outbox.status(DAY, PLAN_HASH, team=team)["status"] == "unknown"
        calls.append(kwargs)
        return Response()
    monkeypatch.setattr(requests, "post", post)
    first = module.push(DAY, team=team)
    assert first["status"] == "sent"
    assert module.push(DAY, team=team)["status"] == "skipped"
    assert len(calls) == 1
    assert calls[0]["json"]["uuid"] == outbox.notification_id(DAY, PLAN_HASH)
    state = outbox.status(DAY, PLAN_HASH, team=team)
    assert state["message_id"] == "om_fixture" and state["evidence"] == "provider_acknowledged"
    text = next((team / ".runs/outbox").rglob("*.json")).read_text()
    assert "fake-secret" not in text and "fake-token" not in text and "fake-recipient" not in text


@pytest.mark.parametrize("response", ["timeout", "bad_json", "no_message_id", "http500", "business_error"])
def test_uncertain_transport_never_automatically_resends(delivery, monkeypatch, response):
    module, team = delivery
    calls = []
    class Response:
        status_code = 500 if response == "http500" else 200
        def json(self):
            if response == "bad_json":
                raise ValueError("invalid JSON")
            return {"code": 1 if response == "business_error" else 0, "data":{}}
    def post(*args, **kwargs):
        calls.append(1)
        if response == "timeout":
            raise requests.Timeout("remote delivery may have succeeded")
        return Response()
    monkeypatch.setattr(requests, "post", post)
    for _ in range(2):
        with pytest.raises(outbox.DeliveryUnknown):
            module.push(DAY, team=team)
    assert calls == [1]


def test_token_failure_is_proven_preflight_not_sent(delivery, monkeypatch):
    module, team = delivery
    monkeypatch.setattr(module, "get_feishu_token", lambda *args: None)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("message request"))
    with pytest.raises(outbox.NotSent):
        module.push(DAY, team=team)
    assert outbox.status(DAY, PLAN_HASH, team=team)["status"] == "retryable"


def test_direct_push_requires_source_evidence_before_network(delivery, monkeypatch):
    module, team = delivery
    def invalid(*args):
        raise ValueError("generation source invalid")
    monkeypatch.setattr(module, "_verify_source", invalid)
    monkeypatch.setattr(module, "_config", lambda: pytest.fail("credentials"))
    with pytest.raises(ValueError, match="source"):
        module.push(DAY, team=team)
    assert not (team / ".runs/outbox").exists()
