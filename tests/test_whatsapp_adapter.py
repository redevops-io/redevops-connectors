"""The WhatsApp Business (Meta Cloud API) adapter against fixtures — connect, execute,
observe, the two invariants (envelope-required-for-writes, no-credential-leak), error
normalization, and the full conformance suite.

Imports the adapter from its module directly (the providers package ``__init__`` is not
edited by this change)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import (
    FakeTransport,
    InMemorySecretResolver,
    Response,
    UnsupportedCapability,
    run_conformance,
)
from redevops_connectors.conformance import ConformanceProbe
from redevops_connectors.providers.whatsapp import WHATSAPP_GRAPH_VERSION, WhatsAppAdapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "whatsapp"

ACCESS_TOKEN = "EAAJB-TEST-WHATSAPP-ACCESS-TOKEN"
PHONE_NUMBER_ID = "106540352242922"
WAMID = "wamid.HBgLMTY1MDU1NTEyMzQVAgARGBI4MjZGRDA0OUE2OTQ3RkEyMzcA"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def resolver() -> InMemorySecretResolver:
    r = InMemorySecretResolver()
    r.put("whatsapp:token", {"access_token": ACCESS_TOKEN})
    return r


@pytest.fixture
def wa_transport() -> FakeTransport:
    t = FakeTransport()
    t.route("POST", "/messages", Response(200, json=load("messages_send.json")))
    # the GET verify-token/id call (connect + health)
    t.route("GET", "fields=display_phone_number", Response(200, json=load("phone_number.json")))
    return t


def _adapter(transport, resolver):
    return WhatsAppAdapter(transport=transport, resolver=resolver,
                           credential_ref="whatsapp:token", phone_number_id=PHONE_NUMBER_ID)


def _send(a):
    return a.execute("chat.message.send", {"to": "+16505551234", "text": "hi"}, envelope=object())


# ── surface ──────────────────────────────────────────────────────────────────
def test_provider_name_and_capability():
    caps = WhatsAppAdapter(transport=FakeTransport(), resolver=InMemorySecretResolver()).capabilities()
    assert WhatsAppAdapter.provider == "whatsapp_business"
    send = next(c for c in caps if c.name == "chat.message.send")
    assert send.tier == 3 and send.write is True


def test_connect_verifies_token_and_number(wa_transport, resolver):
    state = _adapter(wa_transport, resolver).connect({}, "whatsapp:token")
    assert state.connected and state.account_ref == PHONE_NUMBER_ID
    assert state.detail == "ReDevOps Demo"


def test_connect_takes_phone_number_id_from_config(wa_transport, resolver):
    # constructed WITHOUT a phone_number_id; it is supplied via connect()'s config
    a = WhatsAppAdapter(transport=wa_transport, resolver=resolver, credential_ref="whatsapp:token")
    state = a.connect({"phone_number_id": PHONE_NUMBER_ID}, "whatsapp:token")
    assert state.connected
    # and a subsequent send now targets that number id
    r = _send(a)
    assert r.ok
    assert any(f"/{PHONE_NUMBER_ID}/messages" in c["url"] for c in wa_transport.calls)


def test_send_message_returns_the_wamid(wa_transport, resolver):
    r = _send(_adapter(wa_transport, resolver))
    assert r.ok and r.provider_object_id == WAMID


def test_send_hits_the_pinned_graph_version_and_endpoint(wa_transport, resolver):
    _send(_adapter(wa_transport, resolver))
    sent = [c for c in wa_transport.calls if "/messages" in c["url"]]
    assert sent
    url = sent[0]["url"]
    assert f"graph.facebook.com/{WHATSAPP_GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages" in url
    body = json.loads(sent[0]["body"])
    assert body["messaging_product"] == "whatsapp"
    assert body["type"] == "text" and body["text"]["body"] == "hi"
    assert body["to"] == "+16505551234"


def test_write_refuses_without_an_envelope(wa_transport, resolver):
    r = _adapter(wa_transport, resolver).execute(
        "chat.message.send", {"to": "+16505551234", "text": "hi"}, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_unadvertised_capability_raises(wa_transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(wa_transport, resolver).execute("payroll.run", {}, envelope=object())


def test_bearer_token_is_sent_but_never_in_the_result(wa_transport, resolver):
    a = _adapter(wa_transport, resolver)
    r = _send(a)
    sent = [c for c in wa_transport.calls if "/messages" in c["url"]]
    assert sent and sent[0]["headers"].get("Authorization") == f"Bearer {ACCESS_TOKEN}"
    blob = f"{r.provider_object_id}{r.error}{dict(r.data)}"
    assert ACCESS_TOKEN not in blob


def test_observe_roundtrips_a_sent_message(wa_transport, resolver):
    a = _adapter(wa_transport, resolver)
    _send(a)
    assert a.observe(WAMID).found


def test_observe_of_an_unsent_message_is_unknown(wa_transport, resolver):
    # no get-message-by-id: anything this instance did not send is UNKNOWN, not faked
    a = _adapter(wa_transport, resolver)
    assert not a.observe("wamid.SOMETHING_ELSE").found


def test_idempotent_repeat_returns_the_same_wamid(wa_transport, resolver):
    a = _adapter(wa_transport, resolver)
    first, second = _send(a), _send(a)
    assert first.provider_object_id == second.provider_object_id == WAMID


def test_timeout_normalizes_to_a_retryable_failure(resolver):
    t = FakeTransport(timeouts=("/messages",))
    r = _send(_adapter(t, resolver))
    assert not r.ok and r.retryable and "timeout" in r.error


def test_http_429_normalizes_to_a_retryable_failure(resolver):
    t = FakeTransport().route("POST", "/messages", Response(429, json=load("error_reengagement.json")))
    r = _send(_adapter(t, resolver))
    assert not r.ok and r.retryable


def test_http_500_is_retryable(resolver):
    t = FakeTransport().route("POST", "/messages", Response(503, json={}))
    r = _send(_adapter(t, resolver))
    assert not r.ok and r.retryable


def test_graph_error_is_named_and_not_retryable(resolver):
    t = FakeTransport().route("POST", "/messages", Response(400, json=load("error_invalid_token.json")))
    r = _send(_adapter(t, resolver))
    assert not r.ok and not r.retryable
    assert "Invalid OAuth access token" in r.error
    # a Graph error must never leak the token
    assert ACCESS_TOKEN not in r.error


def test_conformance(wa_transport, resolver):
    a = _adapter(wa_transport, resolver)
    probe = ConformanceProbe(
        write_capability="chat.message.send",
        write_request={"to": "+16505551234", "text": "hi"},
        observe_ref=WAMID,
    )
    report = run_conformance(a, probe=probe, resolver=resolver)
    assert report.passed, report.failures()
