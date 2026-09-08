"""Ayrshare — publish one piece of content to many venues through a single API key.

This is the "post to multiple venues at once" provider: one ``content.publish`` call fans
out to any of ~13 networks (X, LinkedIn, Instagram, Facebook, TikTok, YouTube, Reddit,
Telegram, Threads, Bluesky, Pinterest, …). Auth is a bearer API key; every call goes
through the injected transport with the key resolved at the moment of use, so it is tested
against fixtures with no live account and no real key.

Capabilities:
  * ``content.publish`` (tier 3, write) — ``POST /api/post`` with ``{post, platforms[],
    mediaUrls[]}``; returns an Ayrshare post ``id`` plus per-venue ``postIds`` (each with
    its own status + url). The Ayrshare ``id`` is the reconcilable handle.
  * ``content.status`` (tier 1) — ``GET /api/post/{id}`` (also used by ``observe``).

Going live: pass a ``UrllibTransport`` and put the API key behind a ``CredentialRef``
(material ``{"api_key": "…"}``). For posting on behalf of many end-users, Ayrshare issues
per-profile keys — resolve the right one per ``CredentialRef``. Nothing here calls Ayrshare
until then.
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..adapter import (
    BaseAdapter,
    Capability,
    ConnectionState,
    Observation,
    ProviderHealth,
    ProviderResult,
)
from ..transport import Response, TransportTimeout

_API = "https://api.ayrshare.com/api/"


class AyrshareAdapter(BaseAdapter):
    provider = "ayrshare"

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("content.publish", tier=3, write=True),
            Capability("content.status", tier=1, write=False),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        status, data, err = self._get("user", credential_ref=credential_ref)
        if err or status >= 400:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or str(data.get("message", "auth failed")))
        active = data.get("activeSocialAccounts", []) or []
        return ConnectionState(
            provider=self.provider, connected=True,
            account_ref=str(data.get("email", "")), scopes=tuple(active),
            detail=f"{len(active)} venue(s) connected")

    def health(self) -> ProviderHealth:
        status, data, err = self._get("user")
        healthy = not err and status < 400
        return ProviderHealth(healthy=healthy, detail=err or ("ok" if healthy else str(data.get("message", "error"))))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "content.publish":
            platforms: List[str] = list(request.get("platforms", []))
            body: Dict[str, Any] = {"post": request.get("post", ""), "platforms": platforms}
            if request.get("mediaUrls"):
                body["mediaUrls"] = list(request["mediaUrls"])
            status, data, err = self._post("post", body)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            state = str(data.get("status", ""))
            if status < 400 and state in ("success", "scheduled"):
                return ProviderResult(
                    ok=True, capability=capability.name,
                    provider_object_id=str(data.get("id", "")),
                    data={"postIds": data.get("postIds", []), "status": state})
            errs = data.get("errors") or []
            detail = (str(errs[0].get("message", "publish failed")) if errs
                      else str(data.get("message", "publish failed")))
            return ProviderResult(ok=False, capability=capability.name, error=detail,
                                  retryable=status == 429 or status >= 500)

        if capability.name == "content.status":
            return self._status(request.get("id", ""), capability.name)

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    def _status(self, post_id: str, capability: str) -> ProviderResult:
        status, data, err = self._get(f"post/{post_id}")
        if err:
            return ProviderResult(ok=False, capability=capability, error=err, retryable=True)
        found = status < 400 and bool(data.get("id"))
        return ProviderResult(ok=found, capability=capability, provider_object_id=str(data.get("id", "")),
                              data={"status": data.get("status", "")})

    # ── observe (re-fetch the Ayrshare post by id) ──────────────────────────────
    def observe(self, resource_ref: str) -> Observation:
        status, data, err = self._get(f"post/{resource_ref}")
        found = not err and status < 400 and str(data.get("id", "")) == resource_ref
        return Observation(resource_ref=resource_ref, data=data if found else {}, found=found)

    # ── transport helpers (bearer key resolved at use, never logged) ────────────
    def _auth_header(self, credential_ref: str = "") -> Dict[str, str]:
        mat = self._material(credential_ref)
        key = mat.get("api_key") or mat.get("access_token", "")
        return {"Authorization": f"Bearer {key}"}

    def _post(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        headers = {"Content-Type": "application/json", **self._auth_header(credential_ref)}
        try:
            resp: Response = self._transport.request(
                "POST", _API + path, headers=headers, body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""

    def _get(self, path: str, *, credential_ref: str = ""):
        try:
            resp = self._transport.request("GET", _API + path, headers=self._auth_header(credential_ref))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""
