"""Google Calendar — create events and read availability, via Google OAuth2.

Google Calendar authenticates with a Bearer access token from a Google OAuth2
authorization-code grant (see :mod:`redevops_connectors.providers.google_common`). Every
call goes through the injected transport with the token resolved at the moment of use, so
the adapter is tested against fixtures with no live calendar and no real token.

Capabilities:
  * ``calendar.event.create`` (tier 3, write) — ``POST calendars/primary/events`` with
    ``{summary, start, end, attendees}``. Calendar returns the assigned ``{id, htmlLink}``;
    ``id`` is the reconcilable handle.
  * ``calendar.availability.read`` (tier 1) — ``POST freeBusy`` with ``{timeMin, timeMax,
    items:[{id}]}`` returns busy intervals per calendar. It is a POST but reads only, so it
    is not a write and needs no execution envelope.

``observe(event_id)`` re-fetches ``GET calendars/primary/events/{id}`` to confirm the event
id round-trips.

Going live: pass a ``UrllibTransport`` and put the OAuth access token behind a
``CredentialRef`` (material ``{"access_token": "…"}``). Reading availability only needs the
``calendar.readonly`` scope; creating events needs ``calendar.events`` — request the
read-only scope for observe-only missions, since a read scope is markedly less sensitive
than the ability to write events (and invite attendees) as the user. Nothing here calls
Calendar until then.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..adapter import (
    Capability,
    ConnectionState,
    Observation,
    ProviderHealth,
    ProviderResult,
)
from .google_common import GOOGLE_OAUTH, GoogleBearerAdapter, google_error_message

__all__ = ["GoogleCalendarAdapter", "GOOGLE_OAUTH"]

_API = "https://www.googleapis.com/calendar/v3/"

#: The scopes Google Calendar requests: events (write) + readonly (read).
CALENDAR_SCOPES: Tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
)


def _attendees(request: Mapping[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for a in request.get("attendees", []) or []:
        if isinstance(a, str):
            out.append({"email": a})
        elif isinstance(a, Mapping) and a.get("email"):
            out.append({"email": str(a["email"])})
    return out


class GoogleCalendarAdapter(GoogleBearerAdapter):
    provider = "google_calendar"

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("calendar.event.create", tier=3, write=True),
            Capability("calendar.availability.read", tier=1, write=False),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        status, data, err = self._get(_API + "calendars/primary", credential_ref=credential_ref)
        gerr = google_error_message(data)
        if err or status >= 400 or gerr:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or gerr or f"http {status}")
        return ConnectionState(provider=self.provider, connected=True,
                               account_ref=str(data.get("id", "")))

    def health(self) -> ProviderHealth:
        status, data, err = self._get(_API + "calendars/primary")
        healthy = not err and status < 400 and not google_error_message(data)
        return ProviderHealth(healthy=healthy,
                              detail=err or google_error_message(data) or "ok")

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "calendar.event.create":
            body: Dict[str, Any] = {
                "summary": request.get("summary", ""),
                "start": request.get("start", {}),
                "end": request.get("end", {}),
            }
            att = _attendees(request)
            if att:
                body["attendees"] = att
            status, data, err = self._post(_API + "calendars/primary/events", body)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            gerr = google_error_message(data)
            if status >= 400 or gerr:
                return ProviderResult(ok=False, capability=capability.name,
                                      error=gerr or f"http {status}",
                                      retryable=status == 429 or status >= 500)
            eid = str(data.get("id", ""))  # assigned id; none -> "" (observe will be found=False)
            return ProviderResult(ok=True, capability=capability.name, provider_object_id=eid,
                                  data={"id": eid, "htmlLink": str(data.get("htmlLink", ""))})

        if capability.name == "calendar.availability.read":
            body = {
                "timeMin": request.get("timeMin", ""),
                "timeMax": request.get("timeMax", ""),
                "items": request.get("items", [{"id": "primary"}]),
            }
            status, data, err = self._post(_API + "freeBusy", body)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            gerr = google_error_message(data)
            if status >= 400 or gerr:
                return ProviderResult(ok=False, capability=capability.name,
                                      error=gerr or f"http {status}",
                                      retryable=status == 429 or status >= 500)
            return ProviderResult(ok=True, capability=capability.name,
                                  data={"calendars": data.get("calendars", {})})

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── observe (re-fetch the event by its Calendar id) ─────────────────────────
    def observe(self, resource_ref: str) -> Observation:
        if not resource_ref:
            return Observation(resource_ref=resource_ref, found=False)
        status, data, err = self._get(_API + f"calendars/primary/events/{resource_ref}")
        found = not err and status < 400 and str(data.get("id", "")) == resource_ref
        return Observation(resource_ref=resource_ref, data=data if found else {}, found=found)
