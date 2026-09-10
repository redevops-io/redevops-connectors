"""The OAuth2 authorization-code flow against a fixture token response — no live IdP."""
from __future__ import annotations

import pytest

from redevops_connectors import OAuthFlow
from redevops_connectors.oauth import OAuthError
from redevops_connectors.providers import SLACK_OAUTH
from redevops_connectors.transport import FakeTransport, Response

from conftest import CLIENT_SECRET


def _flow(transport, resolver):
    cfg = SLACK_OAUTH(client_id="123.abc", client_secret_ref="slack:client",
                      redirect_uri="https://redevops.io/oauth/slack")
    return OAuthFlow(config=cfg, resolver=resolver, transport=transport)


def test_authorize_url_carries_the_consent_params(slack_transport, resolver):
    url = _flow(slack_transport, resolver).authorize_url(state="s-1")
    assert url.startswith("https://slack.com/oauth/v2/authorize?")
    for part in ("client_id=123.abc", "state=s-1", "redirect_uri=", "scope="):
        assert part in url


def test_authorize_url_merges_extra_provider_params(slack_transport, resolver):
    # provider-specific extras (Google needs these two for a refresh token) are merged in
    from redevops_connectors.oauth import OAuth2Config
    cfg = OAuth2Config(
        provider="google", authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token", client_id="cid",
        client_secret_ref="google:client", scopes=("s1", "s2"),
        redirect_uri="https://redevops.io/cb",
        extra_authorize_params={"access_type": "offline", "prompt": "consent"})
    url = OAuthFlow(config=cfg, resolver=resolver, transport=slack_transport).authorize_url(state="s-1")
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "client_id=cid" in url and "state=s-1" in url          # standard params still present


def test_exchange_code_returns_a_token_grant(slack_transport, resolver):
    grant = _flow(slack_transport, resolver).exchange_code("code-xyz", now=1_000_000.0)
    assert grant.access_token == "xoxb-TEST-ACCESS-TOKEN"
    assert grant.account_ref == "T123"
    assert grant.expires_at == 1_000_000.0 + 43200


def test_exchange_sends_the_client_secret_and_code_on_the_wire(slack_transport, resolver):
    _flow(slack_transport, resolver).exchange_code("code-xyz")
    call = next(c for c in slack_transport.calls if "oauth.v2.access" in c["url"])
    assert "code=code-xyz" in call["body"] and f"client_secret={CLIENT_SECRET}" in call["body"]


def test_token_error_raises(resolver):
    t = FakeTransport().route("POST", "oauth.v2.access", Response(200, json={"ok": False, "error": "invalid_code"}))
    with pytest.raises(OAuthError):
        _flow(t, resolver).exchange_code("bad")
