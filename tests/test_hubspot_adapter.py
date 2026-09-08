"""HubSpot adapter against fixtures — connect, contact upsert (+ duplicate), note create,
observe, the two invariants, and conformance. No live portal, no real token."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers.hubspot import HubSpotAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "hubspot"
TOKEN = "pat-na1-test-hubspot-secret"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("hubspot:token", {"access_token": TOKEN})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "/crm/v3/objects/contacts?", Response(200, json=_load("contacts_list.json")))
    t.route("POST", "/crm/v3/objects/contacts", Response(201, json=_load("contact_created.json")))
    t.route("GET", "/crm/v3/objects/contacts/701", Response(200, json=_load("contact_get.json")))
    t.route("POST", "/crm/v3/objects/notes", Response(201, json=_load("note_created.json")))
    return t


def _adapter(transport, resolver):
    return HubSpotAdapter(transport=transport, resolver=resolver, credential_ref="hubspot:token")


def test_connect_reports_connected(transport, resolver):
    s = _adapter(transport, resolver).connect({}, "hubspot:token")
    assert s.connected and s.provider == "hubspot"


def test_health_is_ok(transport, resolver):
    assert _adapter(transport, resolver).health().healthy


def test_contact_upsert_returns_the_contact_id(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "crm.contact.upsert", {"email": "a@b.com", "firstname": "Jane"}, envelope=object())
    assert r.ok and r.provider_object_id == "701"


def test_contact_upsert_treats_a_duplicate_as_success(resolver):
    t = FakeTransport().route(
        "POST", "/crm/v3/objects/contacts", Response(409, json=_load("contact_duplicate.json")))
    r = _adapter(t, resolver).execute("crm.contact.upsert", {"email": "a@b.com"}, envelope=object())
    assert r.ok and r.provider_object_id == "701"  # existing id parsed out of the message


def test_note_create_returns_the_note_id(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "crm.note.create", {"body": "Followed up with the customer.", "contact_id": "701"},
        envelope=object())
    assert r.ok and r.provider_object_id == "NOTE01" and r.capability == "crm.note.create"


def test_note_create_sends_the_contact_association(transport, resolver):
    _adapter(transport, resolver).execute(
        "crm.note.create", {"body": "hi", "contact_id": "701"}, envelope=object())
    sent = [c for c in transport.calls if "/crm/v3/objects/notes" in c["url"]]
    body = json.loads(sent[0]["body"])
    assoc = body["associations"][0]
    assert assoc["to"]["id"] == "701"
    assert assoc["types"][0]["associationTypeId"] == 202


def test_rate_limit_is_retryable(resolver):
    t = FakeTransport().route(
        "POST", "/crm/v3/objects/contacts", Response(429, json=_load("error_rate_limited.json")))
    r = _adapter(t, resolver).execute("crm.contact.upsert", {"email": "a@b.com"}, envelope=object())
    assert (not r.ok) and r.retryable and "rate limit" in r.error.lower()


def test_timeout_is_retryable(resolver):
    t = FakeTransport()
    t.timeouts = ("/crm/v3/objects/contacts",)
    r = _adapter(t, resolver).execute("crm.contact.upsert", {"email": "a@b.com"}, envelope=object())
    assert (not r.ok) and r.retryable


def test_observe_roundtrips_a_contact(transport, resolver):
    assert _adapter(transport, resolver).observe("701").found


def test_write_refuses_without_envelope(transport, resolver):
    r = _adapter(transport, resolver).execute("crm.contact.upsert", {"email": "a@b.com"}, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_token_is_sent_but_never_in_the_result(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("crm.contact.upsert", {"email": "a@b.com"}, envelope=object())
    sent = [c for c in transport.calls if c["url"].endswith("/crm/v3/objects/contacts")]
    assert sent and f"Bearer {TOKEN}" == sent[0]["headers"]["Authorization"]  # token on the wire
    assert TOKEN not in f"{r.provider_object_id}{r.error}{dict(r.data)}"  # never in the result


def test_oauth_access_token_and_private_app_token_are_interchangeable(transport):
    r = InMemorySecretResolver()
    r.put("hubspot:pat", {"api_key": TOKEN})  # a private-app token under api_key resolves too
    a = HubSpotAdapter(transport=transport, resolver=r, credential_ref="hubspot:pat")
    out = a.execute("crm.contact.upsert", {"email": "a@b.com"}, envelope=object())
    sent = [c for c in transport.calls if c["url"].endswith("/crm/v3/objects/contacts")]
    assert out.ok and sent[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("crm.deal.create", {}, envelope=object())


def test_hubspot_passes_conformance(transport, resolver):
    probe = ConformanceProbe(write_capability="crm.contact.upsert",
                             write_request={"email": "a@b.com"}, observe_ref="701")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]
