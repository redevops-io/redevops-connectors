"""Web research adapter against fixtures — fetch a page, search, invariants. Read-only (no write
capability), so the write-oriented conformance suite doesn't apply; the adapter checks stand in."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.providers import WebAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "web"
KEY = "brave_test_token"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("web:key", {"api_key": KEY})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "https://nutrients.tech", Response(200, text="<html><body>Personalized nutrition.</body></html>"))
    t.route("GET", "/web/search", Response(200, json=_load("brave_search.json")))
    return t


def _adapter(transport, resolver):
    return WebAdapter(transport=transport, resolver=resolver, credential_ref="web:key")


def test_fetch_returns_page_content(transport, resolver):
    r = _adapter(transport, resolver).execute("web.fetch", {"url": "https://nutrients.tech"}, None)
    assert r.ok and "Personalized nutrition" in r.data["content"] and r.provider_object_id == "https://nutrients.tech"


def test_fetch_rejects_a_non_http_url(transport, resolver):
    r = _adapter(transport, resolver).execute("web.fetch", {"url": "file:///etc/passwd"}, None)
    assert not r.ok and "http" in r.error.lower()


def test_fetch_surfaces_an_http_error(resolver):
    t = FakeTransport().route("GET", "https://x.test", Response(404, text="nope"))
    r = _adapter(t, resolver).execute("web.fetch", {"url": "https://x.test"}, None)
    assert not r.ok and "404" in r.error


def test_search_returns_parsed_results(transport, resolver):
    r = _adapter(transport, resolver).execute("web.search", {"query": "nutrients.tech"}, None)
    assert r.ok and len(r.data["results"]) == 2
    assert r.data["results"][0]["url"] == "https://nutrients.tech"


def test_search_requires_a_query(transport, resolver):
    r = _adapter(transport, resolver).execute("web.search", {}, None)
    assert not r.ok and "query" in r.error.lower()


def test_search_sends_the_key_but_never_leaks_it(transport, resolver):
    r = _adapter(transport, resolver).execute("web.search", {"query": "acme"}, None)
    sent = next(c for c in transport.calls if "/web/search" in c["url"])
    assert sent["headers"]["X-Subscription-Token"] == KEY
    assert KEY not in json.dumps({"results": r.data.get("results")})


def test_reads_need_no_envelope():
    a = _adapter(FakeTransport(), InMemorySecretResolver())
    assert all(c.write is False for c in a.capabilities())


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("web.crawl", {}, envelope=object())
