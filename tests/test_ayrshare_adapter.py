"""Ayrshare adapter against fixtures — the multi-venue publish. Connect, publish to many
platforms in one call, observe, the invariants, error handling, and conformance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers import AyrshareAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "ayrshare"
API_KEY = "ayr_test_secret"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("ayrshare:token", {"api_key": API_KEY})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "/api/user", Response(200, json=_load("user.json")))
    t.route("POST", "/api/post", Response(200, json=_load("post_success.json")))
    t.route("GET", "/api/post/AYR01", Response(200, json=_load("post_get.json")))
    return t


def _adapter(transport, resolver):
    return AyrshareAdapter(transport=transport, resolver=resolver, credential_ref="ayrshare:token")


def test_connect_lists_the_active_venues(transport, resolver):
    s = _adapter(transport, resolver).connect({}, "ayrshare:token")
    assert s.connected and set(s.scopes) == {"twitter", "linkedin", "bluesky"}


def test_publish_fans_out_to_many_venues_in_one_call(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "content.publish", {"post": "Today is a great day!", "platforms": ["twitter", "linkedin"]},
        envelope=object())
    assert r.ok and r.provider_object_id == "AYR01"
    assert {p["platform"] for p in r.data["postIds"]} == {"twitter", "linkedin"}


def test_observe_roundtrips_the_post(transport, resolver):
    assert _adapter(transport, resolver).observe("AYR01").found


def test_publish_refuses_without_envelope(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "content.publish", {"post": "hi", "platforms": ["twitter"]}, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_publish_error_is_reported(resolver):
    t = FakeTransport().route("POST", "/api/post",
                              Response(200, json={"status": "error", "errors": [{"message": "no linked accounts"}]}))
    r = _adapter(t, resolver).execute("content.publish", {"post": "hi", "platforms": ["twitter"]}, envelope=object())
    assert not r.ok and "no linked accounts" in r.error


def test_api_key_never_in_the_result(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "content.publish", {"post": "hi", "platforms": ["twitter"]}, envelope=object())
    assert API_KEY not in f"{r.provider_object_id}{r.error}{dict(r.data)}"


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("dm.send", {}, envelope=object())


def test_ayrshare_passes_conformance(transport, resolver):
    probe = ConformanceProbe(
        write_capability="content.publish",
        write_request={"post": "Today is a great day!", "platforms": ["twitter", "linkedin"]},
        observe_ref="AYR01")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]
