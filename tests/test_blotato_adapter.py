"""Blotato adapter against fixtures — connect, content.publish (+ status), observe, the
two invariants, and conformance. No live account, no real key."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers.blotato import BlotatoAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "blotato"
API_KEY = "blotato_test_secret_key"

PUBLISH_REQUEST = {
    "accountId": "98432",
    "text": "Hello world from an AI agent",
    "platform": "twitter",
    "mediaUrls": [],
}


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("blotato:token", {"api_key": API_KEY})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "/v2/users/me/accounts", Response(200, json=_load("accounts.json")))
    t.route("POST", "/v2/posts", Response(201, json=_load("post_created.json")))
    t.route("GET", "/v2/posts/SUB01", Response(200, json=_load("post_status.json")))
    return t


def _adapter(transport, resolver):
    return BlotatoAdapter(transport=transport, resolver=resolver, credential_ref="blotato:token")


def test_connect_reports_the_account(transport, resolver):
    s = _adapter(transport, resolver).connect({}, "blotato:token")
    assert s.connected and s.account_ref == "98432"
    assert "twitter" in s.scopes


def test_publish_returns_the_submission_id(transport, resolver):
    r = _adapter(transport, resolver).execute("content.publish", PUBLISH_REQUEST, envelope=object())
    assert r.ok and r.provider_object_id == "SUB01"


def test_status_reads_the_publish_state(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "content.status", {"postSubmissionId": "SUB01"}, envelope=None)
    assert r.ok and r.data["status"] == "published"


def test_publish_rejected_is_a_named_failure(resolver):
    t = FakeTransport().route("POST", "/v2/posts", Response(400, json=_load("post_rejected.json")))
    r = _adapter(t, resolver).execute("content.publish", PUBLISH_REQUEST, envelope=object())
    assert not r.ok and "not connected" in r.error and not r.retryable


def test_publish_normalizes_429_to_retryable(resolver):
    t = FakeTransport().route("POST", "/v2/posts", Response(429, json={}))
    r = _adapter(t, resolver).execute("content.publish", PUBLISH_REQUEST, envelope=object())
    assert not r.ok and r.retryable


def test_observe_roundtrips_a_submission(transport, resolver):
    assert _adapter(transport, resolver).observe("SUB01").found


def test_write_refuses_without_envelope(transport, resolver):
    r = _adapter(transport, resolver).execute("content.publish", PUBLISH_REQUEST, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_api_key_is_sent_but_never_in_the_result(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("content.publish", PUBLISH_REQUEST, envelope=object())
    sent = [c for c in transport.calls if "/v2/posts" in c["url"]]
    assert sent and sent[0]["headers"]["blotato-api-key"] == API_KEY  # key on the wire
    assert API_KEY not in f"{r.provider_object_id}{r.error}{dict(r.data)}"  # never in the result


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("dm.send", {}, envelope=object())


def test_blotato_passes_conformance(transport, resolver):
    probe = ConformanceProbe(write_capability="content.publish",
                             write_request=PUBLISH_REQUEST, observe_ref="SUB01")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]
