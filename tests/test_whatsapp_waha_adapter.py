"""WhatsApp-via-WAHA adapter against fixtures — send (+ chatId normalization), read, session
status, the two invariants, and conformance. No live gateway, no real key."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers import WhatsAppWahaAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "whatsapp_waha"
KEY = "waha_test_api_key"
BASE = "http://waha.local:3000"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("waha:key", {"api_key": KEY})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "/api/sessions/default", Response(200, json=_load("session_working.json")))
    t.route("POST", "/api/sendText", Response(201, json=_load("send_text.json")))
    t.route("GET", "/api/default/chats/", Response(200, json=_load("messages.json")))
    return t


def _adapter(transport, resolver):
    return WhatsAppWahaAdapter(transport=transport, resolver=resolver, credential_ref="waha:key",
                              base_url=BASE, session="default")


def test_connect_reports_working_session(transport, resolver):
    s = _adapter(transport, resolver).connect({}, "waha:key")
    assert s.connected and s.account_ref == "default"


def test_send_returns_the_message_id(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "chat.message.send", {"to": "+55 11 99999", "text": "olá"}, envelope=object())
    assert r.ok and r.provider_object_id == "true_5511999@c.us_3EB0ABCDEF"


def test_send_normalizes_a_bare_number_to_a_chat_id(transport, resolver):
    _adapter(transport, resolver).execute("chat.message.send", {"to": "5511999", "text": "hi"},
                                          envelope=object())
    sent = next(c for c in transport.calls if "/api/sendText" in c["url"])
    assert json.loads(sent["body"])["chatId"] == "5511999@c.us"          # bare number → @c.us


def test_send_passes_through_an_explicit_chat_id(transport, resolver):
    _adapter(transport, resolver).execute("chat.message.send",
                                          {"chatId": "12036305@g.us", "text": "grp"}, envelope=object())
    sent = next(c for c in transport.calls if "/api/sendText" in c["url"])
    assert json.loads(sent["body"])["chatId"] == "12036305@g.us"         # group id unchanged


def test_read_returns_messages(transport, resolver):
    r = _adapter(transport, resolver).execute("chat.message.read", {"chatId": "5511999@c.us"}, None)
    assert r.ok and len(r.data["messages"]) == 2 and r.data["messages"][0]["body"] == "Oi, tudo bem?"


def test_session_status(transport, resolver):
    r = _adapter(transport, resolver).execute("session.status", {}, None)
    assert r.ok and r.data["status"] == "WORKING"


def test_write_without_envelope_refused(transport, resolver):
    r = _adapter(transport, resolver).execute("chat.message.send", {"to": "1", "text": "x"}, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_send_requires_a_recipient(transport, resolver):
    r = _adapter(transport, resolver).execute("chat.message.send", {"text": "x"}, envelope=object())
    assert not r.ok and "recipient" in r.error.lower()


def test_api_key_sent_but_never_in_result(transport, resolver):
    r = _adapter(transport, resolver).execute("chat.message.send", {"to": "1", "text": "x"}, envelope=object())
    sent = next(c for c in transport.calls if "/api/sendText" in c["url"])
    assert sent["headers"].get("X-Api-Key") == KEY
    assert KEY not in f"{r.provider_object_id}{r.error}{dict(r.data)}"


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("chat.message.delete", {}, envelope=object())


def test_conformance(transport, resolver):
    # observe_ref is the id the fixture send returns; the suite sends first, so observe round-trips
    probe = ConformanceProbe(write_capability="chat.message.send",
                             write_request={"to": "5511999", "text": "hi"},
                             observe_ref="true_5511999@c.us_3EB0ABCDEF")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]
