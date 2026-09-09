"""Klaviyo adapter against fixtures — connect, profile upsert (+ duplicate), event track,
observe, the two invariants, and conformance. No live account, no real key."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers import KlaviyoAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "klaviyo"
API_KEY = "pk_test_klaviyo_secret"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("klaviyo:token", {"api_key": API_KEY})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "/api/accounts", Response(200, json=_load("accounts.json")))
    t.route("POST", "/api/profiles", Response(201, json=_load("profile_created.json")))
    t.route("GET", "/api/profiles/PROF01", Response(200, json=_load("profile_get.json")))
    t.route("POST", "/api/events", Response(202, json={}))
    return t


def _adapter(transport, resolver):
    return KlaviyoAdapter(transport=transport, resolver=resolver, credential_ref="klaviyo:token")


def test_connect_reports_the_account(transport, resolver):
    s = _adapter(transport, resolver).connect({}, "klaviyo:token")
    assert s.connected and s.account_ref == "ACME01"


def test_contact_upsert_returns_the_profile_id(transport, resolver):
    r = _adapter(transport, resolver).execute("contact.upsert", {"email": "a@b.com"}, envelope=object())
    assert r.ok and r.provider_object_id == "PROF01"


def test_contact_upsert_treats_a_duplicate_as_success(resolver):
    t = FakeTransport().route("POST", "/api/profiles", Response(409, json=_load("profile_duplicate.json")))
    r = _adapter(t, resolver).execute("contact.upsert", {"email": "a@b.com"}, envelope=object())
    assert r.ok and r.provider_object_id == "PROF01"  # existing id handed back


def test_event_track_accepts_202_without_an_id(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "email.event.track", {"email": "a@b.com", "metric": "Placed Order"}, envelope=object())
    assert r.ok and r.capability == "email.event.track"


def test_observe_roundtrips_a_profile(transport, resolver):
    assert _adapter(transport, resolver).observe("PROF01").found


def test_write_refuses_without_envelope(transport, resolver):
    r = _adapter(transport, resolver).execute("contact.upsert", {"email": "a@b.com"}, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_api_key_is_sent_but_never_in_the_result(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("contact.upsert", {"email": "a@b.com"}, envelope=object())
    sent = [c for c in transport.calls if "/api/profiles" in c["url"]]
    assert sent and API_KEY in sent[0]["headers"]["Authorization"]  # key on the wire
    assert API_KEY not in f"{r.provider_object_id}{r.error}{dict(r.data)}"  # never in the result


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("sms.send", {}, envelope=object())


def test_klaviyo_passes_conformance(transport, resolver):
    probe = ConformanceProbe(write_capability="contact.upsert",
                             write_request={"email": "a@b.com"}, observe_ref="PROF01")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]
