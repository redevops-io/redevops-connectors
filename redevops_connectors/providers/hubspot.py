"""HubSpot — CRM contacts (+ notes), via a Bearer token (private-app or OAuth).

HubSpot's CRM v3 API authenticates every call with ``Authorization: Bearer <token>`` —
the token is either a **private-app** token or an **OAuth** access token; both are used
identically here. Every call goes through the injected transport with the token resolved
at the moment of use, so the adapter is tested against fixtures with no live portal and
no real token.

Capabilities:
  * ``crm.contact.upsert`` (tier 2, write) — create a contact
    (``POST /crm/v3/objects/contacts`` with ``{"properties": {"email": …}}``); a duplicate
    email resolves to the existing contact id (HubSpot returns 409 CONFLICT with
    ``"Contact already exists. Existing ID: <id>"`` in the message), so the result is
    idempotent and the id re-observes via ``GET /crm/v3/objects/contacts/{id}``.
  * ``crm.note.create`` (tier 2, write) — create a note
    (``POST /crm/v3/objects/notes``) associated to a contact (HUBSPOT_DEFINED note→contact
    association type id 202). HubSpot returns 201 with the note id, so it re-observes too.

Going live: pass a ``UrllibTransport`` and put a HubSpot **private-app token** (Settings →
Integrations → Private Apps, with ``crm.objects.contacts`` read/write scopes) OR an **OAuth
access token** behind a ``CredentialRef`` (material ``{"access_token": "pat-…"}`` or
``{"api_key": "pat-…"}``). Nothing here calls HubSpot until then.
"""
from __future__ import annotations

import json as _json
import re
from datetime import datetime, timezone
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

_API = "https://api.hubapi.com/crm/v3/objects/"
#: HUBSPOT_DEFINED association type id for a note → contact association.
NOTE_TO_CONTACT_TYPE_ID = 202

_EXISTING_ID = re.compile(r"Existing ID:\s*(\S+)")


class HubSpotAdapter(BaseAdapter):
    provider = "hubspot"

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("crm.contact.upsert", tier=2, write=True),
            Capability("crm.note.create", tier=2, write=True),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        status, data, err = self._get("contacts", {"limit": "1"}, credential_ref=credential_ref)
        if err or status >= 400:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or _error_message(data))
        return ConnectionState(provider=self.provider, connected=True, detail="ok")

    def health(self) -> ProviderHealth:
        status, data, err = self._get("contacts", {"limit": "1"})
        healthy = not err and status < 400
        return ProviderHealth(healthy=healthy, detail=err or ("ok" if healthy else _error_message(data)))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "crm.contact.upsert":
            return self._contact_upsert(capability, request)
        if capability.name == "crm.note.create":
            return self._note_create(capability, request)
        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    def _contact_upsert(self, capability: Capability, request: Dict[str, Any]) -> ProviderResult:
        props = dict(request.get("properties") or {})
        for k in ("email", "firstname", "lastname", "phone", "company", "website"):
            v = request.get(k)
            if v:
                props.setdefault(k, v)
        status, data, err = self._post("contacts", {"properties": props})
        if err:
            return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
        if status in (200, 201):
            return ProviderResult(ok=True, capability=capability.name,
                                  provider_object_id=str(data.get("id", "")),
                                  data={"properties": data.get("properties", {})})
        if status == 409:  # already exists — HubSpot names the existing id in the message
            dup = _duplicate_id(data)
            if dup:
                return ProviderResult(ok=True, capability=capability.name, provider_object_id=dup)
        return ProviderResult(ok=False, capability=capability.name, error=_error_message(data),
                              retryable=status == 429 or status >= 500)

    def _note_create(self, capability: Capability, request: Dict[str, Any]) -> ProviderResult:
        props = dict(request.get("properties") or {})
        if request.get("body"):
            props.setdefault("hs_note_body", request["body"])
        props.setdefault("hs_timestamp",
                         request.get("timestamp") or datetime.now(timezone.utc).isoformat())
        body: Dict[str, Any] = {"properties": props}
        contact_id = request.get("contact_id")
        if contact_id:
            body["associations"] = [{
                "to": {"id": str(contact_id)},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": NOTE_TO_CONTACT_TYPE_ID}],
            }]
        status, data, err = self._post("notes", body)
        if err:
            return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
        if status in (200, 201):
            return ProviderResult(ok=True, capability=capability.name,
                                  provider_object_id=str(data.get("id", "")),
                                  data={"properties": data.get("properties", {})})
        return ProviderResult(ok=False, capability=capability.name, error=_error_message(data),
                              retryable=status == 429 or status >= 500)

    # ── observe (a contact re-observes by its object id) ────────────────────────
    def observe(self, resource_ref: str) -> Observation:
        if not resource_ref:
            return Observation(resource_ref=resource_ref, found=False)
        status, data, err = self._get(f"contacts/{resource_ref}", {})
        found = not err and status < 400 and str(data.get("id", "")) == resource_ref
        return Observation(resource_ref=resource_ref, data=data if found else {}, found=found)

    # ── transport helpers (bearer resolved at use, never logged) ────────────────
    def _auth_header(self, credential_ref: str = "") -> Dict[str, str]:
        mat = self._material(credential_ref)
        token = mat.get("access_token") or mat.get("api_key") or ""
        return {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        headers = {"Content-Type": "application/json", **self._auth_header(credential_ref)}
        try:
            resp: Response = self._transport.request(
                "POST", _API + path, headers=headers, body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""

    def _get(self, path: str, query: Mapping[str, Any], *, credential_ref: str = ""):
        import urllib.parse
        url = _API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        try:
            resp = self._transport.request("GET", url, headers=self._auth_header(credential_ref))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""


def _error_message(data: Any) -> str:
    """HubSpot errors are ``{"status":"error","message":…,"category":…}``."""
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])
    return "error"


def _duplicate_id(data: Any) -> str:
    """Parse the existing contact id out of a 409 CONFLICT message:
    ``"Contact already exists. Existing ID: 701"``."""
    if isinstance(data, dict):
        m = _EXISTING_ID.search(str(data.get("message", "")))
        if m:
            return m.group(1)
    return ""
