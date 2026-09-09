"""Gmail — send and read mail for the authenticated user, via Google OAuth2.

Gmail authenticates with a Bearer access token from a Google OAuth2 authorization-code
grant (see :mod:`redevops_connectors.providers.google_common`). Every call goes through
the injected transport with the token resolved at the moment of use, so the adapter is
tested against fixtures with no live mailbox and no real token.

Capabilities:
  * ``email.message.send`` (tier 3, write) — build an RFC 2822 message (To/Subject/body),
    base64url-encode it, and ``POST users/me/messages/send`` with ``{"raw": …}``. Gmail
    returns the assigned ``{id, threadId}``; ``id`` is the reconcilable handle.
  * ``email.message.read`` (tier 1) — ``GET users/me/messages?q=…`` lists matching message
    ids (Gmail search syntax in ``q``); a caller can then fetch each one.

``observe(message_id)`` re-fetches ``GET users/me/messages/{id}`` to confirm the message id
round-trips.

Going live: pass a ``UrllibTransport`` and put the OAuth access token behind a
``CredentialRef`` (material ``{"access_token": "…"}``). Reads only need the ``gmail.readonly``
scope; sending needs ``gmail.send`` — request the read-only scope for observe-only missions,
since a read scope is markedly less sensitive than the ability to send mail as the user.
Nothing here calls Gmail until then.
"""
from __future__ import annotations

import base64
import urllib.parse
from email.message import EmailMessage
from typing import Any, Dict, Mapping, Optional, Tuple

from ..adapter import (
    Capability,
    ConnectionState,
    Observation,
    ProviderHealth,
    ProviderResult,
)
from .google_common import GOOGLE_OAUTH, GoogleBearerAdapter, google_error_message

__all__ = ["GmailAdapter", "GOOGLE_OAUTH"]

_API = "https://gmail.googleapis.com/gmail/v1/"

#: The scopes Gmail requests: send (write) + readonly (read).
GMAIL_SCOPES: Tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)


def _build_raw(request: Mapping[str, Any]) -> str:
    """An RFC 2822 message (To/Subject/body) as a base64url string, as Gmail's ``raw`` wants.
    Gmail fills ``From`` from the authenticated user, so it is left unset here."""
    msg = EmailMessage()
    msg["To"] = str(request.get("to", ""))
    msg["Subject"] = str(request.get("subject", ""))
    msg.set_content(str(request.get("body", "")))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


class GmailAdapter(GoogleBearerAdapter):
    provider = "gmail"

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("email.message.send", tier=3, write=True),
            Capability("email.message.read", tier=1, write=False),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        status, data, err = self._get(_API + "users/me/profile", credential_ref=credential_ref)
        gerr = google_error_message(data)
        if err or status >= 400 or gerr:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or gerr or f"http {status}")
        return ConnectionState(provider=self.provider, connected=True,
                               account_ref=str(data.get("emailAddress", "")))

    def health(self) -> ProviderHealth:
        status, data, err = self._get(_API + "users/me/profile")
        healthy = not err and status < 400 and not google_error_message(data)
        return ProviderHealth(healthy=healthy,
                              detail=err or google_error_message(data) or "ok")

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "email.message.send":
            status, data, err = self._post(_API + "users/me/messages/send",
                                           {"raw": _build_raw(request)})
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            gerr = google_error_message(data)
            if status >= 400 or gerr:
                return ProviderResult(ok=False, capability=capability.name,
                                      error=gerr or f"http {status}",
                                      retryable=status == 429 or status >= 500)
            mid = str(data.get("id", ""))  # assigned id; none -> "" (observe will be found=False)
            return ProviderResult(ok=True, capability=capability.name, provider_object_id=mid,
                                  data={"id": mid, "threadId": str(data.get("threadId", ""))})

        if capability.name == "email.message.read":
            q = str(request.get("q", ""))
            url = _API + "users/me/messages" + ("?" + urllib.parse.urlencode({"q": q}) if q else "")
            status, data, err = self._get(url)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            gerr = google_error_message(data)
            if status >= 400 or gerr:
                return ProviderResult(ok=False, capability=capability.name,
                                      error=gerr or f"http {status}",
                                      retryable=status == 429 or status >= 500)
            return ProviderResult(ok=True, capability=capability.name,
                                  data={"messages": data.get("messages", [])})

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── observe (re-fetch the message by its Gmail id) ──────────────────────────
    def observe(self, resource_ref: str) -> Observation:
        if not resource_ref:
            return Observation(resource_ref=resource_ref, found=False)
        status, data, err = self._get(_API + f"users/me/messages/{resource_ref}")
        found = not err and status < 400 and str(data.get("id", "")) == resource_ref
        return Observation(resource_ref=resource_ref, data=data if found else {}, found=found)
