"""OAuth2 authorization-code flow — the "one-click connect" the wizard hands the user.

The flow is transport-injected and secret-safe: :meth:`OAuthFlow.authorize_url` builds
the consent link the user clicks; :meth:`OAuthFlow.exchange_code` trades the returned code
for a :class:`TokenGrant`. The client secret is resolved from a ``CredentialRef`` at the
moment of the token call and never held or logged. No network happens unless a real
transport is passed — tests drive it with a :class:`~redevops_connectors.transport.FakeTransport`
and a fixture token response, so the whole flow is exercised without a live provider or
real credentials.

Persisting the resulting access/refresh tokens is the caller's job (in the Runtime, the
CredentialBroker stores them behind a new ``CredentialRef``); this module returns the
grant, it does not decide where it lives.
"""
from __future__ import annotations

import json as _json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from .credentials import CredentialRef, SecretResolver
from .transport import Response, Transport


@dataclass(frozen=True)
class OAuth2Config:
    provider: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret_ref: CredentialRef   # resolved at the token call, never inlined
    scopes: Tuple[str, ...] = ()
    redirect_uri: str = ""


@dataclass(frozen=True)
class TokenGrant:
    access_token: str
    refresh_token: str = ""
    scopes: Tuple[str, ...] = ()
    expires_at: float = 0.0            # epoch seconds; 0 => unknown
    account_ref: str = ""             # provider account/team id, when the token call returns it

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() >= self.expires_at


class OAuthError(Exception):
    """The provider rejected the token exchange/refresh."""


@dataclass
class OAuthFlow:
    config: OAuth2Config
    resolver: SecretResolver
    transport: Transport

    # ── step 1: the consent link (no secret involved) ──────────────────────────
    def authorize_url(self, *, state: str) -> str:
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.config.scopes),
            "state": state,
        }
        return self.config.authorize_url + "?" + urllib.parse.urlencode(params)

    # ── step 2: code -> tokens (client secret resolved here, never stored) ──────
    def exchange_code(self, code: str, *, now: Optional[float] = None) -> TokenGrant:
        secret = self.resolver.resolve(self.config.client_secret_ref).get("client_secret", "")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.config.client_id,
            "client_secret": secret,
            "redirect_uri": self.config.redirect_uri,
        }
        return self._token_call(form, now=now)

    def refresh(self, refresh_token: str, *, now: Optional[float] = None) -> TokenGrant:
        secret = self.resolver.resolve(self.config.client_secret_ref).get("client_secret", "")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.config.client_id,
            "client_secret": secret,
        }
        return self._token_call(form, now=now)

    def _token_call(self, form: Mapping[str, str], *, now: Optional[float]) -> TokenGrant:
        body = urllib.parse.urlencode(form).encode("utf-8")
        resp: Response = self.transport.request(
            "POST", self.config.token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            body=body,
        )
        data = resp.json if resp.json is not None else _safe_json(resp.text)
        if resp.status >= 400 or not data or data.get("error"):
            raise OAuthError(f"{self.config.provider} token exchange failed "
                             f"(status {resp.status}): {(data or {}).get('error', 'unknown')}")
        base = now if now is not None else time.time()
        expires_in = data.get("expires_in")
        scope = data.get("scope", "")
        scopes = tuple(scope.split()) if isinstance(scope, str) and scope else self.config.scopes
        return TokenGrant(
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            scopes=scopes,
            expires_at=(base + float(expires_in)) if expires_in else 0.0,
            account_ref=str(data.get("team", {}).get("id", "") if isinstance(data.get("team"), dict)
                            else data.get("account_id", "")),
        )


def _safe_json(text: str) -> dict:
    try:
        obj = _json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}
