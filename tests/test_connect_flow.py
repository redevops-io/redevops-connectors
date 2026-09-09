"""One-click connect orchestration — the full click-Connect → token → stored-ref flow,
driven with no real browser and no live provider. The injected ``open_browser`` plays the
provider: it reads the consent URL and GETs our loopback callback with a code, exactly as
a real redirect would; the token exchange runs against a FakeTransport fixture."""
from __future__ import annotations

import threading
import urllib.parse
import urllib.request

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, OAuth2Config, OAuthFlow, Response
from redevops_connectors.connect_flow import (
    ConnectResult,
    InMemoryCredentialBroker,
    LoopbackConnect,
    embedded_signup_exchange,
)
from redevops_connectors.oauth import OAuthError


def _flow(redirect_uri: str, *, token_response: Response) -> OAuthFlow:
    resolver = InMemorySecretResolver()
    resolver.put("slack:secret", {"client_secret": "shh"})
    transport = FakeTransport().route("POST", "oauth.v2.access", token_response)
    cfg = OAuth2Config(
        provider="slack", authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access", client_id="cid",
        client_secret_ref="slack:secret", scopes=("chat:write",), redirect_uri=redirect_uri,
    )
    return OAuthFlow(config=cfg, resolver=resolver, transport=transport)


def _redirecting_browser(overrides=None):
    """An open_browser that acts like the provider: reads state+redirect_uri off the
    consent URL and calls the loopback callback (echoing state unless overridden)."""
    def browser(url: str) -> None:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        params = {"code": "auth_code_1", "state": q["state"][0]}
        if overrides is not None:
            params = overrides(q)
        cb = q["redirect_uri"][0] + "?" + urllib.parse.urlencode(params)

        def hit():
            try:
                urllib.request.urlopen(cb, timeout=5).read()
            except Exception:
                pass
        threading.Thread(target=hit, daemon=True).start()
    return browser


def test_loopback_connect_stores_a_resolvable_token():
    flow = _flow("http://127.0.0.1:8791/callback",
                 token_response=Response(200, json={"access_token": "xoxb-live",
                                                     "team": {"id": "T42"}, "scope": "chat:write im:write"}))
    broker = InMemoryCredentialBroker()
    res = LoopbackConnect(flow=flow, broker=broker, open_browser=_redirecting_browser(), timeout=10).run()

    assert isinstance(res, ConnectResult)
    assert res.provider == "slack" and res.account_ref == "T42"
    assert res.scopes == ("chat:write", "im:write")
    # the adapter would resolve exactly this ref — token present, never pasted by a human
    assert broker.resolver.resolve(res.credential_ref)["access_token"] == "xoxb-live"


def test_state_mismatch_is_refused():
    flow = _flow("http://127.0.0.1:8792/callback", token_response=Response(200, json={"access_token": "x"}))
    tamper = _redirecting_browser(lambda q: {"code": "c", "state": "not-the-state"})
    with pytest.raises(OAuthError, match="state mismatch"):
        LoopbackConnect(flow=flow, broker=InMemoryCredentialBroker(), open_browser=tamper, timeout=10).run()


def test_provider_error_redirect_surfaces():
    flow = _flow("http://127.0.0.1:8793/callback", token_response=Response(200, json={"access_token": "x"}))
    denied = _redirecting_browser(lambda q: {"error": "access_denied", "error_description": "user said no"})
    with pytest.raises(OAuthError, match="user said no"):
        LoopbackConnect(flow=flow, broker=InMemoryCredentialBroker(), open_browser=denied, timeout=10).run()


def test_timeout_when_user_never_authorizes():
    flow = _flow("http://127.0.0.1:8794/callback", token_response=Response(200, json={"access_token": "x"}))
    with pytest.raises(OAuthError, match="timed out"):
        LoopbackConnect(flow=flow, broker=InMemoryCredentialBroker(),
                        open_browser=lambda url: None, timeout=0.2).run()


def test_embedded_signup_reuses_the_same_exchange():
    # WhatsApp/Meta: code arrives via an in-page widget, exchanged by the same call.
    flow = _flow("", token_response=Response(200, json={"access_token": "EAAG-whatsapp", "account_id": "waba_1"}))
    broker = InMemoryCredentialBroker()
    res = embedded_signup_exchange(flow, "code_from_widget", broker)
    assert res.account_ref == "waba_1"
    assert broker.resolver.resolve(res.credential_ref)["access_token"] == "EAAG-whatsapp"
