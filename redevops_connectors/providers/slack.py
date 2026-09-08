"""Slack — the first reference adapter (OAuth2 v2 authorization-code).

Slack is the approval surface in the Integration Plane's demo, and its OAuth2 flow is the
simplest to stand up, so it is the first provider to prove the SDK end-to-end. Every call
goes through the injected transport with a bearer token resolved at the moment of use, so
the adapter is fully tested against fixtures with no live workspace and no real token.

Going live is a two-part swap, no code change here: pass a ``UrllibTransport`` and put a
real OAuth app's ``client_secret`` (for the flow) and the resulting ``access_token`` (for
calls) behind ``CredentialRef``s in the resolver / CredentialBroker.
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, Mapping, Optional, Tuple

from ..adapter import (
    BaseAdapter,
    Capability,
    ConnectionState,
    Observation,
    ProviderHealth,
    ProviderResult,
)
from ..oauth import OAuth2Config
from ..transport import Response, TransportTimeout

_API = "https://slack.com/api/"

#: The OAuth2 shape for Slack. ``client_id`` and ``client_secret_ref`` are filled per
#: deployment; the secret is resolved only at the token call.
def SLACK_OAUTH(*, client_id: str, client_secret_ref: str, redirect_uri: str,
                scopes: Tuple[str, ...] = ("chat:write", "channels:history", "users:read")) -> OAuth2Config:
    return OAuth2Config(
        provider="slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url=_API + "oauth.v2.access",
        client_id=client_id,
        client_secret_ref=client_secret_ref,
        scopes=scopes,
        redirect_uri=redirect_uri,
    )


class SlackAdapter(BaseAdapter):
    provider = "slack"

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("chat.message.send", tier=3, write=True),
            Capability("chat.message.read", tier=1, write=False),
            Capability("identity.read", tier=1, write=False),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        status, data, err = self._get("auth.test", {}, credential_ref=credential_ref)
        if err or not data.get("ok"):
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or str(data.get("error", "auth failed")))
        return ConnectionState(
            provider=self.provider, connected=True,
            account_ref=str(data.get("team_id", "")),
            detail=str(data.get("url", "")),
        )

    def health(self) -> ProviderHealth:
        status, data, err = self._get("auth.test", {})
        healthy = not err and bool(data.get("ok"))
        return ProviderHealth(healthy=healthy, detail=err or str(data.get("error", "ok")))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "chat.message.send":
            channel, text = request.get("channel", ""), request.get("text", "")
            status, data, err = self._post("chat.postMessage", {"channel": channel, "text": text})
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if not data.get("ok"):
                slack_err = str(data.get("error", "unknown"))
                return ProviderResult(ok=False, capability=capability.name, error=slack_err,
                                      retryable=slack_err in {"rate_limited", "internal_error"})
            ts, ch = str(data.get("ts", "")), str(data.get("channel", channel))
            return ProviderResult(ok=True, capability=capability.name,
                                  provider_object_id=f"{ch}:{ts}", data={"ts": ts, "channel": ch})
        if capability.name == "chat.message.read":
            status, data, err = self._get("conversations.history",
                                          {"channel": request.get("channel", ""), "limit": "5"})
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            msgs = data.get("messages", []) if data.get("ok") else []
            return ProviderResult(ok=bool(data.get("ok")), capability=capability.name,
                                  data={"messages": msgs}, error="" if data.get("ok") else str(data.get("error", "")))
        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── observe ───────────────────────────────────────────────────────────────
    def observe(self, resource_ref: str) -> Observation:
        channel, _, ts = resource_ref.partition(":")
        status, data, err = self._get("conversations.history",
                                      {"channel": channel, "latest": ts, "inclusive": "true", "limit": "1"})
        msgs = data.get("messages", []) if (not err and data.get("ok")) else []
        found = any(str(m.get("ts", "")) == ts for m in msgs)
        return Observation(resource_ref=resource_ref, data={"messages": msgs}, found=found)

    # ── transport helpers (bearer resolved at use, never logged) ────────────────
    def _auth_header(self, credential_ref: str = "") -> Dict[str, str]:
        token = self._material(credential_ref).get("access_token", "")
        return {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        headers = {"Content-Type": "application/json; charset=utf-8", **self._auth_header(credential_ref)}
        try:
            resp: Response = self._transport.request(
                "POST", _API + path, headers=headers, body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return self._normalize(resp)

    def _get(self, path: str, query: Mapping[str, Any], *, credential_ref: str = ""):
        import urllib.parse
        url = _API + path + ("?" + urllib.parse.urlencode(query) if query else "")
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
