"""WhatsApp Business — send a WhatsApp message via Meta's Cloud API (Graph).

This adapter talks to Meta's **official WhatsApp Business Cloud API** (the Graph API at
``https://graph.facebook.com/<version>/``) — the production default for sending WhatsApp
messages. It deliberately does NOT drive any unofficial web-automation / WhatsApp Web
scraping path: those are unsupported by Meta and not permitted for business messaging.

Auth is a bearer access token (``Authorization: Bearer <token>``) resolved at the moment
of use, so the adapter is exercised against fixtures with no live number and no real
token. Two identifiers are involved and only one is a secret:

  * the **access token** IS a secret — resolved from the ``CredentialRef`` material
    (``{"access_token": "EAAJB…"}``) and never logged or returned;
  * the **phone number id** is NOT a secret — it is a stable public identifier for the
    business's sending number. Supply it either as the ``phone_number_id`` constructor
    kwarg, or in ``connect()``'s ``config`` as ``config["phone_number_id"]`` (the latter
    updates the instance). All calls target ``/{phone_number_id}/…``.

Capability:
  * ``chat.message.send`` (tier 3, write) — ``POST /{phone_number_id}/messages`` with
    ``{"messaging_product":"whatsapp","to":<number>,"type":"text","text":{"body":…}}``.
    Meta returns ``{"messages":[{"id":"wamid…"}]}``; that ``wamid`` is the
    ``provider_object_id`` (what verification reconciles against).

Observation: the Cloud API has **no get-message-by-id endpoint** — a sent message's
delivery/read state arrives asynchronously via *message-status webhooks*, not a GET. So
live reconciliation of a WhatsApp send relies on those webhooks. To keep the adapter
honest and the conformance round-trip deterministic, this instance remembers the
``wamid``s it has sent and ``observe()`` reports ``found=True`` only for one of those
(``found=False`` — i.e. UNKNOWN — for anything it did not itself send).

Going live: pass a :class:`UrllibTransport`, put a Meta WhatsApp Business Cloud API
access token behind a ``CredentialRef`` (material ``{"access_token": "…"}``), and supply
the business ``phone_number_id``. Nothing here calls Meta until then.
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, Mapping, Optional, Set, Tuple

from ..adapter import (
    BaseAdapter,
    Capability,
    ConnectionState,
    Observation,
    ProviderHealth,
    ProviderResult,
)
from ..credentials import CredentialRef, SecretResolver
from ..transport import Response, Transport, TransportTimeout

_API = "https://graph.facebook.com/"
#: The Graph API version this adapter pins. Meta versions the Cloud API roughly
#: quarterly; bump deliberately. v25.0 is current as of this writing.
WHATSAPP_GRAPH_VERSION = "v25.0"


class WhatsAppAdapter(BaseAdapter):
    provider = "whatsapp_business"

    def __init__(self, *, transport: Transport, resolver: SecretResolver,
                 credential_ref: CredentialRef = "", phone_number_id: str = "") -> None:
        super().__init__(transport=transport, resolver=resolver, credential_ref=credential_ref)
        #: The business sending number's public id — NOT a secret. May be set here or
        #: overridden by ``connect()``'s ``config["phone_number_id"]``.
        self._phone_number_id = str(phone_number_id)
        #: wamids this instance has sent, so ``observe`` can round-trip honestly (the
        #: Cloud API offers no get-message-by-id; live truth comes from webhooks).
        self._sent_ids: Set[str] = set()

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("chat.message.send", tier=3, write=True),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        pnid = config.get("phone_number_id") or self._phone_number_id
        if pnid:
            self._phone_number_id = str(pnid)
        if not self._phone_number_id:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail="phone_number_id required")
        # GET /{phone_number_id}?fields=display_phone_number,verified_name verifies both
        # the token AND the number id in one call.
        status, data, err = self._get(
            f"{self._phone_number_id}?fields=display_phone_number,verified_name",
            credential_ref=credential_ref)
        graph_err = _graph_error(data)
        if err or status >= 400 or graph_err:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or graph_err or f"http {status}")
        return ConnectionState(
            provider=self.provider, connected=True,
            account_ref=str(data.get("id", self._phone_number_id)),
            detail=str(data.get("verified_name", "")))

    def health(self) -> ProviderHealth:
        if not self._phone_number_id:
            return ProviderHealth(healthy=False, detail="phone_number_id required")
        status, data, err = self._get(
            f"{self._phone_number_id}?fields=display_phone_number,verified_name")
        graph_err = _graph_error(data)
        healthy = not err and status < 400 and not graph_err
        return ProviderHealth(healthy=healthy, detail=err or graph_err or ("ok" if healthy else f"http {status}"))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "chat.message.send":
            if not self._phone_number_id:
                return ProviderResult(ok=False, capability=capability.name,
                                      error="phone_number_id required", retryable=False)
            body = {
                "messaging_product": "whatsapp",
                "to": request.get("to", ""),
                "type": "text",
                "text": {"body": request.get("text", "")},
            }
            status, data, err = self._post(f"{self._phone_number_id}/messages", body)
            if err:  # transport timeout — normalized to a retryable failure
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            graph_err = _graph_error(data)
            if graph_err or status >= 400:
                return ProviderResult(ok=False, capability=capability.name,
                                      error=graph_err or f"http {status}",
                                      retryable=status == 429 or status >= 500)
            msgs = data.get("messages", []) or []
            wamid = str(msgs[0].get("id", "")) if msgs else ""
            if not wamid:
                return ProviderResult(ok=False, capability=capability.name,
                                      error="no message id in response", retryable=False)
            self._sent_ids.add(wamid)
            return ProviderResult(ok=True, capability=capability.name, provider_object_id=wamid,
                                  data={"messages": msgs, "contacts": data.get("contacts", [])})

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── observe ─────────────────────────────────────────────────────────────────
    def observe(self, resource_ref: str) -> Observation:
        # The Cloud API has no get-message-by-id; delivery/read state arrives via
        # message-status webhooks. We can only honestly confirm a wamid THIS instance
        # sent — everything else is UNKNOWN (found=False), never faked.
        found = bool(resource_ref) and resource_ref in self._sent_ids
        return Observation(resource_ref=resource_ref, data={}, found=found)

    # ── transport helpers (bearer token resolved at use, never logged) ──────────
    def _auth_header(self, credential_ref: str = "") -> Dict[str, str]:
        token = self._material(credential_ref).get("access_token", "")
        return {"Authorization": f"Bearer {token}"}

    def _url(self, path: str) -> str:
        return f"{_API}{WHATSAPP_GRAPH_VERSION}/{path}"

    def _post(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        headers = {"Content-Type": "application/json", **self._auth_header(credential_ref)}
        try:
            resp: Response = self._transport.request(
                "POST", self._url(path), headers=headers, body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""

    def _get(self, path: str, *, credential_ref: str = ""):
        try:
            resp = self._transport.request("GET", self._url(path), headers=self._auth_header(credential_ref))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""


def _graph_error(data: Any) -> str:
    """A Graph API error is ``{"error":{"message":…,"type":…,"code":…}}``; surface its
    message (never the token)."""
    if isinstance(data, dict):
        e = data.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e.get("type") or "error")
    return ""
