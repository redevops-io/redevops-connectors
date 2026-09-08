"""Deterministic test rig: fixtures + a routed FakeTransport + an in-memory resolver.

No network, no real credentials — the whole point of the transport seam."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "slack"

ACCESS_TOKEN = "xoxb-TEST-ACCESS-TOKEN"
CLIENT_SECRET = "shhh-client-secret"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def resolver() -> InMemorySecretResolver:
    r = InMemorySecretResolver()
    r.put("slack:token", {"access_token": ACCESS_TOKEN})
    r.put("slack:client", {"client_secret": CLIENT_SECRET})
    return r


@pytest.fixture
def slack_transport() -> FakeTransport:
    t = FakeTransport()
    t.route("POST", "auth.test", Response(200, json=load("auth_test.json")))
    t.route("GET", "auth.test", Response(200, json=load("auth_test.json")))
    t.route("POST", "chat.postMessage", Response(200, json=load("chat_postMessage.json")))
    t.route("GET", "conversations.history", Response(200, json=load("conversations_history.json")))
    t.route("POST", "oauth.v2.access", Response(200, json=load("oauth_token.json")))
    return t
