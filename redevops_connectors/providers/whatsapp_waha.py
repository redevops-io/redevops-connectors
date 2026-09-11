"""WhatsApp via WAHA — the self-hosted WhatsApp HTTP API (multi-number, no Meta approval).

A second, distinct WhatsApp path beside the official Cloud API adapter (``whatsapp.py``): WAHA
(https://waha.devlike.pro) is a self-hosted HTTP gateway that drives a WhatsApp session you connect
by QR — so a deployment can run **multiple numbers** without Meta Business approval (the SMB/LATAM
pattern). It is a documented HTTP API, not web-scraping the adapter drives itself.

Self-hosted, so the API base is configurable: pass ``base_url`` to the constructor or
``config["base_url"]`` at connect. Auth is an optional ``X-Api-Key`` header (WAHA's ``WHATSAPP_API_KEY``);
the ``session`` name (default ``"default"``) is non-secret config. Everything goes through the injected
transport with the key resolved at the moment of use — tested against fixtures, no live gateway.

Capabilities:
  * ``chat.message.send`` (tier 3, write) — send text to a chat (``POST /api/sendText``). ``to`` may
    be a bare number or a full ``…@c.us`` chatId; it is normalized. Returns the WAHA message id.
  * ``chat.message.read`` (tier 1, read) — recent messages of a chat
    (``GET /api/{session}/chats/{chatId}/messages``).
  * ``session.status`` (tier 1, read) — the session's connection state (``GET /api/sessions/{session}``;
    ``WORKING`` = connected, ``SCAN_QR_CODE`` = needs pairing).

Going live: pass a ``UrllibTransport``, point ``base_url`` at your WAHA instance, and (if set) put its
API key behind a ``CredentialRef`` (material ``{"api_key": "…"}``). A send's delivery is reconciled via
WAHA webhooks, not a re-read, so ``observe`` is honestly UNKNOWN (never faked).
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote

from ..adapter import (
    BaseAdapter,
    Capability,
    ConnectionState,
    Observation,
    ProviderHealth,
    ProviderResult,
)
from ..transport import Response, Transport, TransportTimeout
from ..credentials import CredentialRef, SecretResolver

_DEFAULT_BASE = "http://localhost:3000"


def _normalize_base(url: str) -> str:
    return url.rstrip("/")


def _chat_id(value: str) -> str:
    """A bare number → WAHA chatId ``<number>@c.us``; an already-qualified id passes through."""
    v = str(value or "").strip()
    if not v or "@" in v:
        return v
    return f"{v.lstrip('+').replace(' ', '').replace('-', '')}@c.us"


class WhatsAppWahaAdapter(BaseAdapter):
    provider = "whatsapp_waha"

    def __init__(self, *, transport: Transport, resolver: SecretResolver,
                 credential_ref: CredentialRef = "", base_url: Optional[str] = None,
                 session: str = "default") -> None:
        super().__init__(transport=transport, resolver=resolver, credential_ref=credential_ref)
        self._base = _normalize_base(base_url or _DEFAULT_BASE)
        self._session = session or "default"
        #: message ids this instance has sent — so observe() round-trips honestly (found=True only
        #: for our own sends; delivery is otherwise reconciled via WAHA webhooks, never faked).
        self._sent: set = set()

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("chat.message.send", tier=3, write=True),
            Capability("chat.message.read", tier=1, write=False),
            Capability("session.status", tier=1, write=False),
        )

    # ── connection / health ─────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        if config.get("base_url"):
            self._base = _normalize_base(str(config["base_url"]))
        if config.get("session"):
            self._session = str(config["session"])
        status, data, err = self._get(f"/api/sessions/{quote(self._session)}",
                                      credential_ref=credential_ref)
        state = str(data.get("status", "")) if isinstance(data, dict) else ""
        connected = (not err) and status < 400 and state == "WORKING"
        return ConnectionState(provider=self.provider, connected=connected, account_ref=self._session,
                               detail=err or (state or ("ok" if connected else "session not working")))

    def health(self) -> ProviderHealth:
        status, data, err = self._get(f"/api/sessions/{quote(self._session)}")
        state = str(data.get("status", "")) if isinstance(data, dict) else ""
        healthy = (not err) and status < 400 and state in ("WORKING", "SCAN_QR_CODE", "STARTING")
        return ProviderHealth(healthy=healthy, detail=err or (state or "unreachable"))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "chat.message.send":
            chat_id = _chat_id(request.get("chatId") or request.get("to") or "")
            if not chat_id:
                return ProviderResult(ok=False, capability=capability.name,
                                      error="a recipient (to / chatId) is required", retryable=False)
            body = {"session": self._session, "chatId": chat_id, "text": request.get("text", "")}
            status, data, err = self._post("/api/sendText", body)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status < 400:
                mid = _message_id(data)
                if mid:
                    self._sent.add(mid)
                return ProviderResult(ok=True, capability=capability.name, provider_object_id=mid)
            return ProviderResult(ok=False, capability=capability.name, error=_err(data, status),
                                  retryable=status == 429 or status >= 500)

        if capability.name == "chat.message.read":
            chat_id = _chat_id(request.get("chatId") or request.get("to") or "")
            limit = int(request.get("limit", 20))
            status, data, err = self._get(
                f"/api/{quote(self._session)}/chats/{quote(chat_id)}/messages?limit={limit}")
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status < 400:
                msgs = data if isinstance(data, list) else data.get("messages", []) if isinstance(data, dict) else []
                return ProviderResult(ok=True, capability=capability.name, data={"messages": msgs})
            return ProviderResult(ok=False, capability=capability.name, error=_err(data, status),
                                  retryable=status >= 500)

        if capability.name == "session.status":
            status, data, err = self._get(f"/api/sessions/{quote(self._session)}")
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status < 400 and isinstance(data, dict):
                return ProviderResult(ok=True, capability=capability.name,
                                      data={"status": data.get("status", "")})
            return ProviderResult(ok=False, capability=capability.name, error=_err(data, status),
                                  retryable=status >= 500)

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── observe — found=True only for a message THIS instance sent; else UNKNOWN (webhook-reconciled) ──
    def observe(self, resource_ref: str) -> Observation:
        found = bool(resource_ref) and resource_ref in self._sent
        return Observation(resource_ref=resource_ref, found=found)

    # ── transport helpers (key resolved at use, never logged) ───────────────────
    def _headers(self, credential_ref: str = "") -> Dict[str, str]:
        mat = self._material(credential_ref)
        key = mat.get("api_key") or mat.get("access_token", "")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["X-Api-Key"] = key
        return headers

    def _post(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        try:
            resp: Response = self._transport.request(
                "POST", self._base + path, headers=self._headers(credential_ref),
                body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json if resp.json is not None else {}), ""

    def _get(self, path: str, *, credential_ref: str = ""):
        try:
            resp = self._transport.request("GET", self._base + path, headers=self._headers(credential_ref))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json if resp.json is not None else {}), ""


def _message_id(data: Any) -> str:
    if isinstance(data, dict):
        mid = data.get("id")
        if isinstance(mid, dict):
            return str(mid.get("_serialized") or mid.get("id") or "")
        if mid:
            return str(mid)
    return ""


def _err(data: Any, status: int) -> str:
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or f"HTTP {status}")
    return f"HTTP {status}"
