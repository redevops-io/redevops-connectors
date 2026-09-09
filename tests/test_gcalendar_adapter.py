"""Google Calendar adapter against fixtures — connect, event create, availability read
(freeBusy), observe, the two invariants (envelope-required-for-writes, no-credential-leak),
error normalization, and conformance. No live calendar, no real token."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.oauth import OAuth2Config
from redevops_connectors.providers.gcalendar import CALENDAR_SCOPES, GOOGLE_OAUTH, GoogleCalendarAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "gcalendar"
ACCESS_TOKEN = "ya29.TEST-GOOGLE-ACCESS-TOKEN"

_EVENT = {
    "summary": "Quarterly review",
    "start": {"dateTime": "2026-09-10T15:00:00-04:00"},
    "end": {"dateTime": "2026-09-10T15:30:00-04:00"},
    "attendees": ["dana@example.com"],
}
_FREEBUSY = {"timeMin": "2026-09-10T00:00:00Z", "timeMax": "2026-09-11T00:00:00Z",
             "items": [{"id": "primary"}]}


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("gcal:token", {"access_token": ACCESS_TOKEN})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    # observe (specific event id) must be matched before the calendars/primary route
    t.route("GET", "calendars/primary/events/EVT01", Response(200, json=_load("event_get.json")))
    t.route("GET", "calendars/primary", Response(200, json=_load("calendar_primary.json")))
    t.route("POST", "calendars/primary/events", Response(200, json=_load("event_created.json")))
    t.route("POST", "freeBusy", Response(200, json=_load("freebusy.json")))
    return t


def _adapter(transport, resolver):
    return GoogleCalendarAdapter(transport=transport, resolver=resolver, credential_ref="gcal:token")


def test_google_oauth_shape():
    cfg = GOOGLE_OAUTH(client_id="cid", client_secret_ref="gcal:client",
                       redirect_uri="https://app/redirect", scopes=CALENDAR_SCOPES)
    assert isinstance(cfg, OAuth2Config)
    assert cfg.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert cfg.token_url == "https://oauth2.googleapis.com/token"
    assert "https://www.googleapis.com/auth/calendar.events" in cfg.scopes


def test_connect_reports_the_calendar(transport, resolver):
    s = _adapter(transport, resolver).connect({}, "gcal:token")
    assert s.connected and s.account_ref == "acme.ops@example.com"


def test_event_create_returns_the_event_id(transport, resolver):
    r = _adapter(transport, resolver).execute("calendar.event.create", _EVENT, envelope=object())
    assert r.ok and r.provider_object_id == "EVT01"
    assert r.data["htmlLink"].startswith("https://")


def test_availability_read_returns_busy_intervals(transport, resolver):
    r = _adapter(transport, resolver).execute("calendar.availability.read", _FREEBUSY, envelope=None)
    assert r.ok and r.data["calendars"]["primary"]["busy"]


def test_observe_roundtrips_the_created_event(transport, resolver):
    assert _adapter(transport, resolver).observe("EVT01").found


def test_write_refuses_without_envelope(transport, resolver):
    r = _adapter(transport, resolver).execute("calendar.event.create", _EVENT, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_bearer_token_is_sent_but_never_in_the_result(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("calendar.event.create", _EVENT, envelope=object())
    sent = [c for c in transport.calls if "calendars/primary/events" in c["url"]]
    assert sent and sent[0]["headers"].get("Authorization") == f"Bearer {ACCESS_TOKEN}"  # token on the wire
    blob = f"{r.provider_object_id}{r.error}{dict(r.data)}"
    assert ACCESS_TOKEN not in blob  # ...but never in the result


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("calendar.event.delete", {}, envelope=object())


def test_timeout_normalizes_to_a_retryable_failure(resolver):
    t = FakeTransport(timeouts=("calendars/primary/events",))
    r = _adapter(t, resolver).execute("calendar.event.create", _EVENT, envelope=object())
    assert not r.ok and r.retryable and "timeout" in r.error


def test_http_500_normalizes_to_a_retryable_failure(resolver):
    t = FakeTransport().route("POST", "calendars/primary/events", Response(503, json={"error": {"code": 503, "message": "Backend Error"}}))
    r = _adapter(t, resolver).execute("calendar.event.create", _EVENT, envelope=object())
    assert not r.ok and r.retryable


def test_google_error_is_named_and_not_retryable(resolver):
    t = FakeTransport().route("POST", "calendars/primary/events",
                              Response(403, json={"error": {"code": 403, "message": "Insufficient Permission", "status": "PERMISSION_DENIED"}}))
    r = _adapter(t, resolver).execute("calendar.event.create", _EVENT, envelope=object())
    assert not r.ok and not r.retryable and r.error == "Insufficient Permission"


def test_create_without_assigned_id_leaves_object_id_empty(resolver):
    t = FakeTransport().route("POST", "calendars/primary/events", Response(200, json={}))
    r = _adapter(t, resolver).execute("calendar.event.create", _EVENT, envelope=object())
    assert r.ok and r.provider_object_id == ""


def test_gcalendar_passes_conformance(transport, resolver):
    probe = ConformanceProbe(write_capability="calendar.event.create",
                             write_request=_EVENT, observe_ref="EVT01")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]
