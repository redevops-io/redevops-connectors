"""One-click connect orchestration — the runtime fetches the token itself.

This is the piece that actually *solves* the integration problem for a non-technical
user: they click **Connect**, the provider's own consent screen opens, and the token
comes back on its own — nobody opens a developer console and copy-pastes a secret.

The correct mechanism is **not** to script a login to a provider's developer portal and
scrape a token (brittle, against every provider's ToS, and it would need the user's
console password). It is the standard OAuth2 authorization-code flow against a
**ReDevOps-owned OAuth app** — one app registered per provider, once, by us — where the
user only ever authorizes on the provider's real consent screen:

    click Connect
      → open the provider consent URL (ReDevOps app's client_id + requested scopes)
      → user approves on the provider's own page
      → provider redirects to a loopback callback we're listening on (127.0.0.1)
      → we catch the ``?code=…&state=…``, verify state (CSRF), exchange code → token
      → store the token behind a fresh CredentialRef via the broker
      → SetupState → CONNECTED, with zero secrets typed

:class:`LoopbackConnect` runs exactly that. Everything external is injected — the
browser-open is a callable, the network is the transport seam — so the whole flow is
exercised in tests with no browser and no live provider: the test's ``open_browser``
just GETs the callback URL with a fake code, and ``exchange_code`` runs against a
:class:`~redevops_connectors.transport.FakeTransport`.

**WhatsApp / Meta** is the same exchange with a different front door: Meta's *Embedded
Signup* JS widget hands back an authorization ``code`` that is exchanged for a token by
the very same :meth:`OAuthFlow.exchange_code` call — only the step that obtains the code
differs (an in-page widget instead of a loopback redirect). :func:`embedded_signup_exchange`
covers that case so WhatsApp reuses this module rather than a bespoke path.
"""
from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, Optional, Tuple

from .credentials import CredentialRef, InMemorySecretResolver
from .oauth import OAuthError, OAuthFlow, TokenGrant

_DEFAULT_REDIRECT = "http://127.0.0.1:8765/callback"

_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Connected</title>"
    b"<body style='font:16px system-ui;margin:4rem;text-align:center'>"
    b"<h2>&#10003; Connected to %s</h2>"
    b"<p>You can close this tab and return to ReDevOps.</p>"
)
_FAIL_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Connect failed</title>"
    b"<body style='font:16px system-ui;margin:4rem;text-align:center'>"
    b"<h2>Couldn't complete the connection</h2><p>%s</p>"
)


# ── where the grant is stored ───────────────────────────────────────────────────
class CredentialBroker:
    """Stores an OAuth grant behind a fresh, opaque :class:`CredentialRef`.

    In the Runtime this is a real secret store; the adapter later resolves the ref at the
    moment of use and the raw token never travels through application code.
    """

    def store(self, provider: str, grant: TokenGrant) -> CredentialRef:  # pragma: no cover - protocol
        raise NotImplementedError


@dataclass
class InMemoryCredentialBroker(CredentialBroker):
    """A broker backed by an :class:`InMemorySecretResolver` — for tests and local demos.

    The same resolver is handed to the adapter, so ``connect`` → adapter is wired end to
    end without a real secret store.
    """

    resolver: InMemorySecretResolver = field(default_factory=InMemorySecretResolver)
    _n: int = 0

    def store(self, provider: str, grant: TokenGrant) -> CredentialRef:
        self._n += 1
        ref = f"{provider}:oauth:{self._n}"
        material = {"access_token": grant.access_token, "api_key": grant.access_token}
        if grant.refresh_token:
            material["refresh_token"] = grant.refresh_token
        self.resolver.put(ref, material)
        return ref


@dataclass(frozen=True)
class ConnectResult:
    provider: str
    credential_ref: CredentialRef
    account_ref: str = ""
    scopes: Tuple[str, ...] = ()


# ── the loopback callback listener (one-shot, localhost only) ────────────────────
def _make_handler(path: str, box: Dict[str, str], provider: str):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(parsed.query)
            if "error" in q:
                box["error"] = q.get("error_description", q["error"])[0]
                self._reply(400, _FAIL_PAGE % box["error"].encode("utf-8", "replace"))
                return
            box["code"] = q.get("code", [""])[0]
            box["state"] = q.get("state", [""])[0]
            if box["code"]:
                self._reply(200, _DONE_PAGE % provider.encode("utf-8"))
            else:
                box["error"] = "no authorization code in redirect"
                self._reply(400, _FAIL_PAGE % b"No authorization code was returned.")

        def _reply(self, status: int, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):  # keep the callback silent (and never log the code)
            pass

    return _Handler


def _parse_redirect(redirect_uri: str) -> Tuple[str, int, str]:
    u = urllib.parse.urlparse(redirect_uri or _DEFAULT_REDIRECT)
    return (u.hostname or "127.0.0.1", u.port or 80, u.path or "/callback")


@dataclass
class LoopbackConnect:
    """Runs the full click-Connect → token flow and returns a stored CredentialRef.

    The redirect URI (which must match the one registered on the ReDevOps OAuth app) also
    tells us which loopback host/port/path to listen on. ``open_browser`` and the flow's
    transport are the only things that touch the outside world, so tests inject both.
    """

    flow: OAuthFlow
    broker: CredentialBroker
    open_browser: Callable[[str], None] = webbrowser.open
    timeout: float = 180.0
    state_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24)

    def run(self) -> ConnectResult:
        host, port, path = _parse_redirect(self.flow.config.redirect_uri)
        state = self.state_factory()
        box: Dict[str, str] = {}
        httpd = HTTPServer((host, port), _make_handler(path, box, self.flow.config.provider))
        httpd.timeout = 1.0
        try:
            worker = threading.Thread(target=self._serve_until_capture, args=(httpd, box), daemon=True)
            worker.start()
            self.open_browser(self.flow.authorize_url(state=state))
            worker.join(self.timeout + 2)
        finally:
            httpd.server_close()

        if box.get("error"):
            raise OAuthError(f"{self.flow.config.provider} connect failed: {box['error']}")
        if not box.get("code"):
            raise OAuthError(f"{self.flow.config.provider} connect timed out — no authorization received")
        if box.get("state") != state:
            raise OAuthError("state mismatch — possible CSRF; connection refused")

        grant = self.flow.exchange_code(box["code"])
        ref = self.broker.store(self.flow.config.provider, grant)
        return ConnectResult(provider=self.flow.config.provider, credential_ref=ref,
                             account_ref=grant.account_ref, scopes=grant.scopes)

    def _serve_until_capture(self, httpd: HTTPServer, box: Dict[str, str]) -> None:
        deadline = time.time() + self.timeout
        while not box.get("code") and not box.get("error") and time.time() < deadline:
            httpd.handle_request()  # returns every httpd.timeout secs even with no request


# ── Meta / WhatsApp Embedded Signup: same exchange, in-page code ─────────────────
def embedded_signup_exchange(flow: OAuthFlow, code: str, broker: CredentialBroker,
                             *, now: Optional[float] = None) -> ConnectResult:
    """Exchange a code from Meta's Embedded Signup widget and store the grant.

    Meta's WhatsApp onboarding returns the authorization ``code`` through an in-page JS
    widget rather than a loopback redirect, but the token exchange is identical — so
    WhatsApp reuses :meth:`OAuthFlow.exchange_code` here instead of a separate code path.
    """
    grant = flow.exchange_code(code, now=now)
    ref = broker.store(flow.config.provider, grant)
    return ConnectResult(provider=flow.config.provider, credential_ref=ref,
                         account_ref=grant.account_ref, scopes=grant.scopes)
