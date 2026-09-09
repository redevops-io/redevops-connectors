"""Postiz adapter against fixtures — connect, publish across channels, a named provider
error, observe an integration, the two invariants, self-host base_url, timeout, and
conformance. No live account, no real key."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers.postiz import POSTIZ_PUBLIC_API, PostizAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "postiz"
API_KEY = "postiz_test_secret_key"
SELF_HOST = "https://postiz.internal.redevops.io/public/v1"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("postiz:token", {"api_key": API_KEY})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "/public/v1/integrations", Response(200, json=_load("integrations.json")))
    t.route("POST", "/public/v1/posts", Response(201, json=_load("post_created.json")))
    return t


def _adapter(transport, resolver, **kw):
    return PostizAdapter(transport=transport, resolver=resolver,
                         credential_ref="postiz:token", **kw)


_PUBLISH = {"integrations": ["INT01", "INT02"], "content": "Shipping day.", "type": "now"}


def test_connect_reports_the_channels(transport, resolver):
    s = _adapter(transport, resolver).connect({}, "postiz:token")
    assert s.connected and s.account_ref == "CUST01"
    assert s.scopes == ("x", "linkedin") and "2 channel" in s.detail


def test_content_publish_returns_the_post_id(transport, resolver):
    r = _adapter(transport, resolver).execute("content.publish", _PUBLISH, envelope=object())
    assert r.ok and r.provider_object_id == "POST01"
    assert r.data["postIds"] == ["POST01", "POST02"]


def test_publish_error_is_named_not_ok(resolver):
    t = FakeTransport().route("POST", "/public/v1/posts", Response(400, json=_load("post_error.json")))
    r = _adapter(t, resolver).execute("content.publish", _PUBLISH, envelope=object())
    assert not r.ok and not r.retryable and "content" in r.error.lower()


def test_publish_5xx_is_retryable(resolver):
    t = FakeTransport().route("POST", "/public/v1/posts", Response(503, json={"message": "down"}))
    r = _adapter(t, resolver).execute("content.publish", _PUBLISH, envelope=object())
    assert not r.ok and r.retryable


def test_timeout_is_retryable(resolver):
    t = FakeTransport()
    t.timeouts = ("/public/v1/posts",)
    r = _adapter(t, resolver).execute("content.publish", _PUBLISH, envelope=object())
    assert not r.ok and r.retryable and r.error == "timeout"


def test_observe_roundtrips_an_integration(transport, resolver):
    assert _adapter(transport, resolver).observe("INT01").found


def test_observe_missing_integration_is_not_found(transport, resolver):
    assert not _adapter(transport, resolver).observe("NOPE").found


def test_write_refuses_without_envelope(transport, resolver):
    r = _adapter(transport, resolver).execute("content.publish", _PUBLISH, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_api_key_is_sent_raw_but_never_in_the_result(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("content.publish", _PUBLISH, envelope=object())
    sent = [c for c in transport.calls if "/public/v1/posts" in c["url"]]
    assert sent and sent[0]["headers"]["Authorization"] == API_KEY  # raw key, no "Bearer"
    assert API_KEY not in f"{r.provider_object_id}{r.error}{dict(r.data)}"  # never in the result


def test_self_host_base_url_from_constructor(resolver):
    t = FakeTransport().route("POST", "/public/v1/posts", Response(201, json=_load("post_created.json")))
    a = _adapter(t, resolver, base_url=SELF_HOST)
    a.execute("content.publish", _PUBLISH, envelope=object())
    assert t.calls[-1]["url"].startswith(SELF_HOST + "/")
    assert not t.calls[-1]["url"].startswith(POSTIZ_PUBLIC_API)


def test_self_host_base_url_from_connect_config(resolver):
    t = FakeTransport().route("GET", "/public/v1/integrations", Response(200, json=_load("integrations.json")))
    a = _adapter(t, resolver)
    a.connect({"base_url": SELF_HOST}, "postiz:token")
    assert t.calls[-1]["url"].startswith(SELF_HOST + "/")


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("content.status", {}, envelope=object())


def test_postiz_passes_conformance(transport, resolver):
    probe = ConformanceProbe(write_capability="content.publish",
                             write_request=_PUBLISH, observe_ref="INT01")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]
