"""Blotato — publish content to social venues through one API, built for AI agents.

Blotato is a multi-venue social-posting API positioned for AI-agent builders: one
``content.publish`` call submits a post to a connected account on any of its networks
(X/Twitter, LinkedIn, Facebook, Instagram, TikTok, YouTube, Threads, Bluesky, Pinterest).
Auth is a single ``blotato-api-key`` header; every call goes through the injected
transport with the key resolved at the moment of use, so the adapter is tested against
fixtures with no live account and no real key.

Publishing is asynchronous: ``POST /v2/posts`` returns a ``postSubmissionId`` (the
reconcilable handle) and the real publish happens in the background — you poll
``GET /v2/posts/{postSubmissionId}`` for ``status`` (``in-progress`` | ``scheduled`` |
``published`` | ``failed``) and the resulting ``publicUrl``.

Capabilities:
  * ``content.publish`` (tier 3, write) — ``POST /v2/posts`` with a ``{post: {accountId,
    content: {text, mediaUrls[], platform}, target: {targetType}}}`` body (optional
    root-level ``scheduledTime`` / ``useNextFreeSlot``). Returns ``postSubmissionId`` —
    the handle verification re-observes. No id back ⇒ UNKNOWN, never faked.
  * ``content.status`` (tier 1) — ``GET /v2/posts/{postSubmissionId}`` (also used by
    ``observe``) for the publish status + public URL.

Going live: pass a ``UrllibTransport`` and put the API key behind a ``CredentialRef``
(material ``{"api_key": "…"}`` — the Blotato key from Settings → API). Resolve the target
``accountId`` up front from ``GET /v2/users/me/accounts``. Nothing here calls Blotato
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

_API = "https://backend.blotato.com/v2/"


class BlotatoAdapter(BaseAdapter):
    provider = "blotato"

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("content.publish", tier=3, write=True),
            Capability("content.status", tier=1, write=False),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        status, data, err = self._get("users/me/accounts", credential_ref=credential_ref)
        if err or status >= 400:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or _first_error(data))
        items = data.get("items", []) if isinstance(data, dict) else []
        account_ref = str(items[0].get("id", "")) if items else ""
        venues = tuple(str(a.get("platform", "")) for a in items if a.get("platform"))
        return ConnectionState(
            provider=self.provider, connected=True, account_ref=account_ref,
            scopes=venues, detail=f"{len(items)} account(s) connected")

    def health(self) -> ProviderHealth:
        status, data, err = self._get("users/me/accounts")
        healthy = not err and status < 400
        return ProviderHealth(healthy=healthy,
                              detail=err or ("ok" if healthy else _first_error(data)))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "content.publish":
            platform = str(request.get("platform", ""))
            media: List[str] = list(request.get("mediaUrls", []))
            body: Dict[str, Any] = {
                "post": {
                    "accountId": str(request.get("accountId", "")),
                    "content": {
                        "text": request.get("text", ""),
                        "mediaUrls": media,
                        "platform": platform,
                    },
                    "target": {"targetType": str(request.get("targetType") or platform)},
                }
            }
            if request.get("scheduledTime"):
                body["scheduledTime"] = request["scheduledTime"]
            if request.get("useNextFreeSlot"):
                body["useNextFreeSlot"] = bool(request["useNextFreeSlot"])

            status, data, err = self._post("posts", body)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            sub_id = str(data.get("postSubmissionId", ""))
            if status < 400 and sub_id:
                return ProviderResult(
                    ok=True, capability=capability.name, provider_object_id=sub_id,
                    data={"scheduledTime": data.get("scheduledTime", "")})
            return ProviderResult(ok=False, capability=capability.name, error=_first_error(data),
                                  retryable=status == 429 or status >= 500)

        if capability.name == "content.status":
            return self._status(str(request.get("postSubmissionId") or request.get("id", "")),
                                 capability.name)

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    def _status(self, submission_id: str, capability: str) -> ProviderResult:
        if not submission_id:
            return ProviderResult(ok=False, capability=capability, error="missing postSubmissionId")
        status, data, err = self._get(f"posts/{submission_id}")
        if err:
            return ProviderResult(ok=False, capability=capability, error=err, retryable=True)
        found = status < 400 and bool(data.get("status"))
        return ProviderResult(
            ok=found, capability=capability,
            provider_object_id=submission_id if found else "",
            data={"status": data.get("status", ""), "publicUrl": data.get("publicUrl", "")},
            error="" if found else _first_error(data),
            retryable=status == 429 or status >= 500)

    # ── observe (re-fetch the submission by its id) ─────────────────────────────
    def observe(self, resource_ref: str) -> Observation:
        if not resource_ref:
            return Observation(resource_ref=resource_ref, found=False)
        status, data, err = self._get(f"posts/{resource_ref}")
        found = not err and status < 400 and bool(data.get("status"))
        return Observation(resource_ref=resource_ref, data=data if found else {}, found=found)

    # ── transport helpers (key resolved at use, never logged) ───────────────────
    def _headers(self, credential_ref: str = "") -> Dict[str, str]:
        mat = self._material(credential_ref)
        key = mat.get("api_key") or mat.get("access_token", "")
        return {
            "blotato-api-key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        try:
            resp: Response = self._transport.request(
                "POST", _API + path, headers=self._headers(credential_ref),
                body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""

    def _get(self, path: str, *, credential_ref: str = ""):
        try:
            resp = self._transport.request("GET", _API + path, headers=self._headers(credential_ref))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""


def _first_error(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "error")
        if isinstance(err, str) and err:
            return err
        if data.get("message"):
            return str(data["message"])
    return "error"
