"""The loopback callback and the token call, at their edges: text from the query string is shown as text, the listener binds to loopback
only, and an answer without a token is not a grant."""
import threading
import urllib.error
import urllib.request

import pytest

from redevops_connectors import InMemorySecretResolver
from redevops_connectors.connect_flow import InMemoryCredentialBroker, LoopbackConnect, _parse_redirect
from redevops_connectors.oauth import OAuth2Config, OAuthError, OAuthFlow
from redevops_connectors.transport import FakeTransport, Response


def flow(token_response, redirect="http://127.0.0.1:8791/callback"):
    resolver = InMemorySecretResolver()
    ref = resolver.put("app", {"client_secret": "s3cr3t-value"})
    cfg = OAuth2Config(provider="slack", authorize_url="https://provider.example.com/authorize", token_url="https://provider.example.com/token",
                       client_id="cid", client_secret_ref=ref, scopes=("chat:write",), redirect_uri=redirect)
    return OAuthFlow(cfg, resolver, FakeTransport().route("POST", "/token", token_response))


def test_the_error_text_of_the_callback_is_shown_as_text_never_as_markup():
    seen = {}

    def browser(url):
        try:
            urllib.request.urlopen("http://127.0.0.1:8791/callback?error=access_denied&error_description=%3Cscript%3Ealert(1)%3C%2Fscript%3E", timeout=5)
        except urllib.error.HTTPError as e:
            seen["status"], seen["body"], seen["csp"] = e.code, e.read().decode(), e.headers.get("Content-Security-Policy")
    with pytest.raises(OAuthError):
        LoopbackConnect(flow(Response(200, json={"access_token": "t"})), InMemoryCredentialBroker(), open_browser=browser, timeout=5).run()
    assert seen["status"] == 400 and "<script>" not in seen["body"] and "&lt;script&gt;alert(1)&lt;/script&gt;" in seen["body"]
    assert seen["csp"] and "default-src 'none'" in seen["csp"]


@pytest.mark.parametrize("uri", ["http://0.0.0.0:8765/callback", "http://192.168.1.10:8765/callback", "http://example.com/callback", "https://127.0.0.1:8765/callback",
                                 "http://[::]:8765/callback"])
def test_the_listener_refuses_an_address_that_is_not_loopback_http(uri):
    with pytest.raises(OAuthError):
        _parse_redirect(uri)


@pytest.mark.parametrize("uri, want", [("http://127.0.0.1:8765/callback", ("127.0.0.1", 8765, "/callback")), ("http://localhost:9000/cb", ("localhost", 9000, "/cb")),
                                       ("http://[::1]:8765/callback", ("::1", 8765, "/callback")), ("", ("127.0.0.1", 8765, "/callback"))])
def test_loopback_addresses_are_accepted(uri, want):
    assert _parse_redirect(uri) == want


@pytest.mark.parametrize("answer", [Response(200, json={"token_type": "bearer"}), Response(200, json={"access_token": ""}), Response(200, json={"access_token": None}),
                                    Response(200, text="not json")])
def test_an_answer_without_an_access_token_is_not_a_grant(answer):
    with pytest.raises(OAuthError) as e:
        flow(answer).exchange_code("code")
    assert "s3cr3t-value" not in str(e.value)


def test_an_expiry_that_is_not_a_number_does_not_break_the_grant():
    g = flow(Response(200, json={"access_token": "t", "expires_in": "soon"})).exchange_code("code", now=1000.0)
    assert g.access_token == "t" and g.expires_at == 0.0
    g = flow(Response(200, json={"access_token": "t", "expires_in": 60})).exchange_code("code", now=1000.0)
    assert g.expires_at == 1060.0
