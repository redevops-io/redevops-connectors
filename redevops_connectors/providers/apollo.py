"""Apollo (apollo.io) — cold outreach via sequences, through a warmed connected mailbox.

Apollo authenticates every call with an API key sent in the ``X-Api-Key`` header (no OAuth).
Bodies and responses are JSON. Every call goes through the injected transport with the key
resolved at the moment of use, so the adapter is tested against fixtures with no live account
and no real key.

Capabilities:
  * ``contact.upsert`` (tier 2, write) — create a contact (``POST /contacts``) →
    ``provider_object_id = contact.id``.
  * ``outreach.sequence.configure`` (tier 3, write) — build a sequence's first email step
    (``POST /emailer_steps``) then set the email content on the TEMPLATE
    (``PUT /emailer_templates/<id>``). Two Apollo gotchas are enforced here:
      - ``wait_mode`` MUST be one of ``day`` / ``minute`` / ``hour`` — Apollo rejects
        ``second`` / ``immediately`` with ``{"error": "Wait mode must be valid"}``. The
        adapter defaults to ``day`` and coerces any invalid value to ``day`` (never sends an
        invalid one).
      - the subject / body live on the ``emailer_template`` endpoint, not on the step.
  * ``outreach.enroll`` (tier 3, write) — add contacts to a sequence
    (``POST /emailer_campaigns/<id>/add_contact_ids``) through the configured sender mailbox.
  * ``outreach.sequence.activate`` (tier 4, write, **non-automatable**) — the physical
    capability result: activating a sequence in Apollo is UI-only. There is no activate
    endpoint (``PUT /emailer_campaigns/{id} {active:true}`` is ignored), so this capability
    is advertised as ``automatable=False`` / ``human_required=True`` /
    ``execution_strategy="provider_ui"``; a planner routes it to a human gate. ``execute``
    for it never pretends to succeed — it returns ``ok=False`` with the physical result.
  * ``outreach.observe`` (tier 1, read) — read a contact's enrollment status
    (``GET /contacts/<id>``) from ``contact.contact_campaign_statuses[0]``.
  * ``contact.enrich`` (tier 1, read) — match/enrich a person from partial identifiers
    (``POST /people/match``) → the enriched person record. A retrieve, not a mutation, so no
    execution envelope is required; writing the result into a CRM is a separate governed capability.
  * ``company.enrich`` (tier 1, read) — enrich an organization by domain
    (``GET /organizations/enrich``).

Going live: pass a ``UrllibTransport`` and put the API key behind a ``CredentialRef``
(material ``{"api_key": "…"}`` or ``{"access_token": "…"}``); pass ``sequence_id`` and
``sender_account_id`` (NOT secrets — config) via ctor/config. Nothing here calls Apollo
until the live transport is wired.
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
from ..credentials import CredentialRef, SecretResolver
from ..transport import Response, Transport, TransportTimeout

_API = "https://api.apollo.io/v1"

#: Apollo rejects any wait_mode outside this set (e.g. "second"/"immediately") with
#: ``{"error": "Wait mode must be valid"}``. The adapter coerces to the default instead.
_VALID_WAIT_MODES = ("day", "minute", "hour")
_DEFAULT_WAIT_MODE = "day"


class ApolloAdapter(BaseAdapter):
    provider = "apollo"

    def __init__(self, *, transport: Transport, resolver: SecretResolver,
                 credential_ref: CredentialRef = "", sequence_id: str = "",
                 sender_account_id: str = "") -> None:
        super().__init__(transport=transport, resolver=resolver, credential_ref=credential_ref)
        self._sequence_id = sequence_id
        self._sender_account_id = sender_account_id

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("contact.upsert", tier=2, write=True),
            Capability("outreach.sequence.configure", tier=3, write=True),
            Capability("outreach.enroll", tier=3, write=True),
            # The physical capability result: Apollo activation is UI-only, so the
            # planner routes it to a human gate BEFORE attempting execution.
            Capability("outreach.sequence.activate", tier=4, write=True,
                       automatable=False, human_required=True,
                       execution_strategy="provider_ui"),
            Capability("outreach.observe", tier=1, write=False),
            # Enrichment/research — reads: they retrieve data about a person/company, they do not
            # mutate anything at Apollo (writing it into a CRM is a separate governed capability),
            # so no execution envelope is required. The returned data is PII/customer-content and is
            # classified on egress like any other read.
            Capability("contact.enrich", tier=1, write=False),
            Capability("company.enrich", tier=1, write=False),
        )

    # ── connection / health (a free auth/health probe — no credits) ──────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        if config.get("sequence_id"):
            self._sequence_id = str(config["sequence_id"])
        if config.get("sender_account_id"):
            self._sender_account_id = str(config["sender_account_id"])
        status, data, err = self._get("/auth/health", credential_ref=credential_ref)
        connected = (not err) and status < 400 and _is_healthy(data)
        return ConnectionState(provider=self.provider, connected=connected,
                               detail=err or _msg(data) or ("ok" if connected else "auth failed"))

    def health(self) -> ProviderHealth:
        status, data, err = self._get("/auth/health")
        healthy = (not err) and status < 400 and _is_healthy(data)
        return ProviderHealth(healthy=healthy, detail=err or _msg(data) or ("ok" if healthy else "error"))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "contact.upsert":
            return self._contact_upsert(capability, request)
        if capability.name == "outreach.sequence.configure":
            return self._sequence_configure(capability, request)
        if capability.name == "outreach.enroll":
            return self._enroll(capability, request)
        if capability.name == "outreach.sequence.activate":
            return self._activate(capability, request)
        if capability.name == "outreach.observe":
            return self._observe_status(capability, request.get("contact_id") or request.get("id") or "")
        if capability.name == "contact.enrich":
            return self._contact_enrich(capability, request)
        if capability.name == "company.enrich":
            return self._company_enrich(capability, request)
        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── contact.enrich (people/match — retrieve, don't mutate) ───────────────────
    def _contact_enrich(self, capability: Capability, request: Dict[str, Any]) -> ProviderResult:
        body = {k: v for k, v in {
            "first_name": request.get("first_name"), "last_name": request.get("last_name"),
            "name": request.get("name"), "email": request.get("email"),
            "domain": request.get("domain"), "organization_name": request.get("organization_name"),
        }.items() if v}
        status, data, err = self._post("/people/match", body)
        if err:
            return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
        person = data.get("person") if isinstance(data, dict) else None
        if status < 400 and isinstance(person, dict):
            return ProviderResult(ok=True, capability=capability.name,
                                  provider_object_id=str(person.get("id", "")), data=person)
        if status < 400:                       # a clean no-match is an honest empty, not an error
            return ProviderResult(ok=True, capability=capability.name, data={})
        return ProviderResult(ok=False, capability=capability.name,
                              error=_msg(data) or "enrich failed", retryable=status == 429 or status >= 500)

    # ── company.enrich (organizations/enrich by domain) ──────────────────────────
    def _company_enrich(self, capability: Capability, request: Dict[str, Any]) -> ProviderResult:
        domain = str(request.get("domain", ""))
        if not domain:
            return ProviderResult(ok=False, capability=capability.name,
                                  error="a domain is required", retryable=False)
        status, data, err = self._get(f"/organizations/enrich?domain={quote(domain)}")
        if err:
            return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
        org = data.get("organization") if isinstance(data, dict) else None
        if status < 400 and isinstance(org, dict):
            return ProviderResult(ok=True, capability=capability.name,
                                  provider_object_id=str(org.get("id", "")), data=org)
        if status < 400:
            return ProviderResult(ok=True, capability=capability.name, data={})
        return ProviderResult(ok=False, capability=capability.name,
                              error=_msg(data) or "enrich failed", retryable=status == 429 or status >= 500)

    # ── contact.upsert ──────────────────────────────────────────────────────────
    def _contact_upsert(self, capability: Capability, request: Dict[str, Any]) -> ProviderResult:
        body = {k: v for k, v in {
            "email": request.get("email"),
            "first_name": request.get("first_name"),
            "last_name": request.get("last_name"),
        }.items() if v}
        status, data, err = self._post("/contacts", body)
        if err:
            return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
        contact = data.get("contact") if isinstance(data, dict) else None
        cid = str(contact.get("id", "")) if isinstance(contact, dict) else ""
        if status in (200, 201) and cid:
            return ProviderResult(ok=True, capability=capability.name, provider_object_id=cid,
                                  data={"email": (contact or {}).get("email")})
        return ProviderResult(ok=False, capability=capability.name,
                              error=_msg(data) or "contact upsert failed",
                              retryable=status == 429 or status >= 500)

    # ── outreach.sequence.configure (build step, then set content on the TEMPLATE) ─
    def _sequence_configure(self, capability: Capability, request: Dict[str, Any]) -> ProviderResult:
        sequence_id = str(request.get("sequence_id") or self._sequence_id or "")
        if not sequence_id:
            return ProviderResult(ok=False, capability=capability.name,
                                  error="sequence_id required", retryable=False)
        # GOTCHA: coerce an invalid wait_mode to a valid one — never send "second".
        wait_mode = str(request.get("wait_mode") or _DEFAULT_WAIT_MODE)
        if wait_mode not in _VALID_WAIT_MODES:
            wait_mode = _DEFAULT_WAIT_MODE

        step_body = {
            "emailer_campaign_id": sequence_id,
            "priority": 1,
            "position": 1,
            "type": "auto_email",
            "wait_time": 0,
            "wait_mode": wait_mode,
        }
        status, data, err = self._post("/emailer_steps", step_body)
        if err:
            return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
        step = data.get("emailer_step") if isinstance(data, dict) else None
        if status not in (200, 201) or not isinstance(step, dict) or not step.get("id"):
            return ProviderResult(ok=False, capability=capability.name,
                                  error=_msg(data) or "emailer step create failed",
                                  retryable=status == 429 or status >= 500)
        step_id = str(step["id"])
        template_id = _first_template_id(data)
        if not template_id:
            return ProviderResult(ok=False, capability=capability.name,
                                  error="no emailer_template on the created step", retryable=False)

        # GOTCHA: email content lives on the emailer_template endpoint, not on the step.
        subject = request.get("subject", "")
        body_html = request.get("body_html", "")
        t_status, t_data, t_err = self._put(
            f"/emailer_templates/{template_id}",
            {"subject": subject, "body_html": body_html})
        if t_err:
            return ProviderResult(ok=False, capability=capability.name, error=t_err, retryable=True)
        if t_status not in (200, 201):
            return ProviderResult(ok=False, capability=capability.name,
                                  error=_msg(t_data) or "emailer template update failed",
                                  retryable=t_status == 429 or t_status >= 500)
        return ProviderResult(ok=True, capability=capability.name, provider_object_id=step_id,
                              data={"step_id": step_id, "template_id": template_id, "subject": subject})

    # ── outreach.enroll (add contacts through the configured sender mailbox) ──────
    def _enroll(self, capability: Capability, request: Dict[str, Any]) -> ProviderResult:
        sequence_id = str(request.get("sequence_id") or self._sequence_id or "")
        if not sequence_id:
            return ProviderResult(ok=False, capability=capability.name,
                                  error="sequence_id required", retryable=False)
        ids = request.get("contact_ids")
        if not ids and request.get("contact_id"):
            ids = [request["contact_id"]]
        contact_ids = [str(c) for c in (ids or [])]
        if not contact_ids:
            return ProviderResult(ok=False, capability=capability.name,
                                  error="contact_ids required", retryable=False)
        body = {
            "contact_ids": contact_ids,
            "emailer_campaign_id": sequence_id,
            "send_email_from_email_account_id": self._sender_account_id,
            "sequence_active": True,
        }
        status, data, err = self._post(f"/emailer_campaigns/{sequence_id}/add_contact_ids", body)
        if err:
            return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
        if status in (200, 201):
            oid = _first_enrolled_id(data) or contact_ids[0]
            return ProviderResult(ok=True, capability=capability.name, provider_object_id=str(oid),
                                  data={"sequence_id": sequence_id, "contact_ids": contact_ids})
        return ProviderResult(ok=False, capability=capability.name,
                              error=_msg(data) or "enroll failed",
                              retryable=status == 429 or status >= 500)

    # ── outreach.sequence.activate (UI-only — never claims success) ──────────────
    def _activate(self, capability: Capability, request: Dict[str, Any]) -> ProviderResult:
        return ProviderResult(
            ok=False, capability=capability.name,
            error="activation requires the provider UI — not available via API",
            retryable=False,
            data={"execution_strategy": "provider_ui", "human_required": True})

    # ── outreach.observe ──────────────────────────────────────────────────────────
    def _observe_status(self, capability: Capability, contact_id: str) -> ProviderResult:
        obs = self.observe(str(contact_id))
        return ProviderResult(ok=obs.found, capability=capability.name,
                              provider_object_id=str(contact_id), data=dict(obs.data),
                              error="" if obs.found else "enrollment status not found")

    def observe(self, resource_ref: str) -> Observation:
        if not resource_ref:
            return Observation(resource_ref=resource_ref, found=False)
        status, data, err = self._get(f"/contacts/{resource_ref}")
        contact = data.get("contact") if isinstance(data, dict) else None
        if err or status >= 400 or not isinstance(contact, dict):
            return Observation(resource_ref=resource_ref, found=False)
        statuses = contact.get("contact_campaign_statuses") or []
        first = statuses[0] if isinstance(statuses, list) and statuses else {}
        enrollment = first.get("status") if isinstance(first, dict) else None
        inactive_reason = first.get("inactive_reason") if isinstance(first, dict) else None
        return Observation(resource_ref=resource_ref, found=bool(enrollment),
                           data={"status": enrollment, "inactive_reason": inactive_reason})

    # ── transport helpers (key resolved at use, never logged) ────────────────────
    def _headers(self, credential_ref: str = "") -> Dict[str, str]:
        mat = self._material(credential_ref)
        key = mat.get("api_key") or mat.get("access_token", "")
        return {
            "X-Api-Key": key,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "Accept": "application/json",
        }

    def _get(self, path: str, *, credential_ref: str = ""):
        try:
            resp = self._transport.request("GET", _API + path, headers=self._headers(credential_ref))
        except TransportTimeout:
            return 0, {}, "timeout"
        return self._normalize(resp)

    def _post(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        try:
            resp: Response = self._transport.request(
                "POST", _API + path, headers=self._headers(credential_ref),
                body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return self._normalize(resp)

    def _put(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        try:
            resp: Response = self._transport.request(
                "PUT", _API + path, headers=self._headers(credential_ref),
                body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return self._normalize(resp)

    @staticmethod
    def _normalize(resp: Response):
        data = resp.json if resp.json is not None else {}
        if resp.status == 429 or resp.status >= 500:
            return resp.status, data, f"http {resp.status}"
        return resp.status, data, ""


def _is_healthy(data: Any) -> bool:
    if isinstance(data, dict):
        return bool(data.get("is_logged_in") or data.get("healthy"))
    return False


def _first_template_id(data: Any) -> str:
    if isinstance(data, dict):
        touches = data.get("emailer_touches")
        if isinstance(touches, list) and touches and isinstance(touches[0], dict):
            tid = touches[0].get("emailer_template_id")
            if tid:
                return str(tid)
        templates = data.get("emailer_templates")
        if isinstance(templates, list) and templates and isinstance(templates[0], dict):
            tid = templates[0].get("id")
            if tid:
                return str(tid)
    return ""


def _first_enrolled_id(data: Any) -> str:
    if isinstance(data, dict):
        contacts = data.get("contacts")
        if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
            c = contacts[0]
            statuses = c.get("contact_campaign_statuses") or []
            if isinstance(statuses, list) and statuses and isinstance(statuses[0], dict):
                sid = statuses[0].get("id")
                if sid:
                    return str(sid)
            if c.get("id"):
                return str(c["id"])
    return ""


def _msg(data: Any) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    return ""
