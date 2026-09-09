"""Shared Google plumbing for the Google vertical (Gmail + Calendar).

Both Google adapters authenticate identically — an OAuth2 authorization-code grant that
yields a Bearer access token sent on every API call — and normalize errors the same way.
The only thing that differs between them is the scope set and the API host. This module
holds the shared pieces so each adapter declares just its own capabilities and endpoints:

  * :func:`GOOGLE_OAUTH` — the OAuth2 shape (Google's authorize/token endpoints), with the
    per-adapter scopes passed in;
  * :func:`google_error_message` — pulls the human-readable message out of Google's
    ``{"error": {...}}`` error envelope;
  * :class:`GoogleBearerAdapter` — Bearer transport helpers (token resolved at the moment
    of use, never held or logged) with 429/5xx normalized to a retryable failure.

Going live: create an OAuth client in the Google Cloud Console (APIs & Services →
Credentials → OAuth client ID, "Web application"), enable the Gmail API and the Google
Calendar API on the project, and register the redirect URI. Put the client secret behind a
``CredentialRef`` (material ``{"client_secret": "…"}``) for the token exchange and the
resulting access token behind another (material ``{"access_token": "…"}``) for calls.
Request the narrowest scopes a mission needs: the read scopes (``gmail.readonly``,
``calendar.readonly``) are far less sensitive than the send/write scopes (``gmail.send``,
``calendar.events``), so ask for a read-only scope wherever a mission only observes.
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, Mapping, Tuple

from ..adapter import BaseAdapter
from ..oauth import OAuth2Config
from ..transport import Response, TransportTimeout

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


#: The OAuth2 shape for Google. ``client_id`` and ``client_secret_ref`` are filled per
#: deployment; the secret is resolved only at the token call. ``scopes`` is passed by each
#: adapter (Gmail vs Calendar request different scopes).
def GOOGLE_OAUTH(*, client_id: str, client_secret_ref: str, redirect_uri: str,
                 scopes: Tuple[str, ...] = (
                     "https://www.googleapis.com/auth/gmail.send",
                     "https://www.googleapis.com/auth/gmail.readonly",
                 )) -> OAuth2Config:
    return OAuth2Config(
        provider="google",
        authorize_url=_AUTHORIZE_URL,
        token_url=_TOKEN_URL,
        client_id=client_id,
        client_secret_ref=client_secret_ref,
        scopes=scopes,
        redirect_uri=redirect_uri,
    )


def google_error_message(data: Any) -> str:
    """The message from Google's ``{"error": {code, message, status, …}}`` envelope, or ""."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or "error")
    return ""


class GoogleBearerAdapter(BaseAdapter):
    """Bearer-token transport shared by the Google adapters. The access token is resolved
    at the moment of use and never held on the adapter or written to a result. 429/5xx are
    normalized to a retryable failure; other responses come back with their body for the
    adapter to interpret (including Google's ``{"error": {...}}`` envelope)."""

    def _auth_header(self, credential_ref: str = "") -> Dict[str, str]:
        token = self._material(credential_ref).get("access_token", "")
        return {"Authorization": f"Bearer {token}"}

    def _post(self, url: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        headers = {"Content-Type": "application/json", **self._auth_header(credential_ref)}
        try:
            resp: Response = self._transport.request(
                "POST", url, headers=headers, body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return self._normalize(resp)

    def _get(self, url: str, *, credential_ref: str = ""):
        try:
            resp = self._transport.request("GET", url, headers=self._auth_header(credential_ref))
        except TransportTimeout:
            return 0, {}, "timeout"
        return self._normalize(resp)

    @staticmethod
    def _normalize(resp: Response):
        data = resp.json if resp.json is not None else {}
        if resp.status == 429 or resp.status >= 500:
            return resp.status, data, f"http {resp.status}"
        return resp.status, data, ""
