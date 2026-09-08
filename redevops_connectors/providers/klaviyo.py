"""Klaviyo — email/SMS marketing, via a private API key (no OAuth).

Klaviyo authenticates with a private API key in the ``Authorization: Klaviyo-API-Key …``
header, uses the JSON:API media type, and requires a dated ``revision`` header — all set
here. Every call goes through the injected transport with the key resolved at the moment
of use, so the adapter is tested against fixtures with no live account and no real key.

Capabilities:
  * ``contact.upsert`` (tier 2, write) — create a profile (``POST /api/profiles``); a
    duplicate resolves to the existing profile id (Klaviyo returns it on 409), so the
    result is idempotent and the created id re-observes via ``GET /api/profiles/{id}``.
  * ``email.event.track`` (tier 3, write) — fire an event (``POST /api/events``) that
    triggers a Klaviyo flow (i.e. sends the email). Klaviyo returns 202 with no id, so a
    tracked event is not directly re-observable — reconciliation is UNKNOWN, never faked.

Going live: pass a ``UrllibTransport`` and put the private key behind a ``CredentialRef``
(material ``{"api_key": "pk_…"}``). Nothing here calls Klaviyo until then.
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
from ..transport import Response, TransportTimeout

_API = "https://a.klaviyo.com/api/"
#: Klaviyo pins its API by a dated revision header; bump deliberately.
KLAVIYO_REVISION = "2026-07-15"


class KlaviyoAdapter(BaseAdapter):
    provider = "klaviyo"

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("contact.upsert", tier=2, write=True),
            Capability("email.event.track", tier=3, write=True),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        status, data, err = self._get("accounts", credential_ref=credential_ref)
        if err or status >= 400:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or _first_error(data))
        rows = data.get("data", []) if isinstance(data, dict) else []
        account_id = str(rows[0].get("id", "")) if rows else ""
        return ConnectionState(provider=self.provider, connected=True, account_ref=account_id)

    def health(self) -> ProviderHealth:
        status, data, err = self._get("accounts")
        healthy = not err and status < 400
        return ProviderHealth(healthy=healthy, detail=err or ("ok" if healthy else _first_error(data)))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "contact.upsert":
            attrs = {k: v for k, v in {
                "email": request.get("email"),
                "phone_number": request.get("phone_number"),
                "first_name": request.get("first_name"),
                "last_name": request.get("last_name"),
            }.items() if v}
            body = {"data": {"type": "profile", "attributes": attrs}}
            status, data, err = self._post("profiles", body)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status in (200, 201):
                pid = str(data.get("data", {}).get("id", ""))
                return ProviderResult(ok=True, capability=capability.name, provider_object_id=pid)
            if status == 409:  # already exists — Klaviyo hands back the existing id
                dup = _duplicate_id(data)
                if dup:
                    return ProviderResult(ok=True, capability=capability.name, provider_object_id=dup)
            return ProviderResult(ok=False, capability=capability.name, error=_first_error(data),
                                  retryable=status == 429 or status >= 500)

        if capability.name == "email.event.track":
            body = {"data": {"type": "event", "attributes": {
                "properties": request.get("properties", {}),
                "metric": {"data": {"type": "metric",
                                    "attributes": {"name": request.get("metric", "Custom Event")}}},
                "profile": {"data": {"type": "profile",
                                     "attributes": {"email": request.get("email", "")}}},
            }}}
            status, data, err = self._post("events", body)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status == 202:  # accepted; Klaviyo returns no id for a tracked event
                return ProviderResult(ok=True, capability=capability.name,
                                      provider_object_id=str(request.get("unique_id", "")))
            return ProviderResult(ok=False, capability=capability.name, error=_first_error(data),
                                  retryable=status == 429 or status >= 500)

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── observe (profiles are re-observable; tracked events are not) ────────────
    def observe(self, resource_ref: str) -> Observation:
        if not resource_ref:
            return Observation(resource_ref=resource_ref, found=False)
        status, data, err = self._get(f"profiles/{resource_ref}")
        found = not err and status < 400 and str(data.get("data", {}).get("id", "")) == resource_ref
        return Observation(resource_ref=resource_ref, data=data if found else {}, found=found)

    # ── transport helpers (key resolved at use, never logged) ───────────────────
    def _headers(self, credential_ref: str = "") -> Dict[str, str]:
        mat = self._material(credential_ref)
        key = mat.get("api_key") or mat.get("access_token", "")
        return {
            "Authorization": f"Klaviyo-API-Key {key}",
            "revision": KLAVIYO_REVISION,
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/vnd.api+json",
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
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            return str(errs[0].get("detail") or errs[0].get("title") or "error")
    return "error"


def _duplicate_id(data: Any) -> str:
    if isinstance(data, dict):
        for e in data.get("errors", []) or []:
            dup = (e.get("meta") or {}).get("duplicate_profile_id")
            if dup:
                return str(dup)
    return ""
