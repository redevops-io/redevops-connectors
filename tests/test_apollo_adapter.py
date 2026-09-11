"""Apollo adapter against fixtures — contact upsert, sequence configure (wait_mode +
template-endpoint gotchas), enroll, the UI-only activation physical result, observe,
invariants, conformance. No live account, no real key."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers import ApolloAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "apollo"
KEY = "apollo_test_secret_key"
CONTACT_ID = "cont_1"
SEQUENCE_ID = "seq_1"
SENDER_ID = "mailbox_1"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("apollo:key", {"api_key": KEY})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "/auth/health", Response(200, json=_load("auth_health.json")))
    t.route("POST", "/contacts", Response(200, json=_load("contact_created.json")))
    t.route("GET", "/contacts/cont_1", Response(200, json=_load("contact_status.json")))
    t.route("POST", "/emailer_steps", Response(200, json=_load("step_created.json")))
    t.route("PUT", "/emailer_templates/tmpl_1", Response(200, json=_load("template_updated.json")))
    t.route("POST", "/emailer_campaigns/seq_1/add_contact_ids", Response(200, json=_load("enroll_response.json")))
    t.route("POST", "/people/match", Response(200, json=_load("people_match.json")))
    t.route("GET", "/organizations/enrich", Response(200, json=_load("organization_enrich.json")))
    return t


def _adapter(transport, resolver):
    return ApolloAdapter(transport=transport, resolver=resolver, credential_ref="apollo:key",
                         sequence_id=SEQUENCE_ID, sender_account_id=SENDER_ID)


def _cap(adapter, name):
    return next(c for c in adapter.capabilities() if c.name == name)


# ── connection ──────────────────────────────────────────────────────────────
def test_connect_ok_when_logged_in(transport, resolver):
    assert _adapter(transport, resolver).connect({}, "apollo:key").connected is True


# ── contact.upsert ───────────────────────────────────────────────────────────
def test_contact_upsert_returns_contact_id(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "contact.upsert", {"email": "t@example.com", "first_name": "Test", "last_name": "Lead"},
        envelope=object())
    assert r.ok and r.provider_object_id == CONTACT_ID


# ── outreach.sequence.configure — the two gotchas as first-class assertions ───
def test_sequence_configure_uses_a_valid_wait_mode_and_sets_template(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("outreach.sequence.configure",
                  {"subject": "Quick question", "body_html": "<p>Hi</p>", "wait_mode": "hour"},
                  envelope=object())
    assert r.ok and r.provider_object_id == "step_1"
    assert r.data["template_id"] == "tmpl_1"

    step_post = next(c for c in transport.calls if c["method"] == "POST" and "/emailer_steps" in c["url"])
    assert json.loads(step_post["body"])["wait_mode"] in ("day", "minute", "hour")

    # the template content is set via PUT /emailer_templates/<id>, NOT on the step
    tmpl_put = next(c for c in transport.calls if c["method"] == "PUT" and "/emailer_templates/tmpl_1" in c["url"])
    tbody = json.loads(tmpl_put["body"])
    assert tbody["subject"] == "Quick question" and tbody["body_html"] == "<p>Hi</p>"


def test_sequence_configure_coerces_invalid_wait_mode_never_sends_second(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("outreach.sequence.configure",
                  {"subject": "S", "body_html": "B", "wait_mode": "second"}, envelope=object())
    assert r.ok
    step_post = next(c for c in transport.calls if c["method"] == "POST" and "/emailer_steps" in c["url"])
    sent = json.loads(step_post["body"])
    assert sent["wait_mode"] == "day"  # coerced
    # Apollo never receives an invalid wait_mode
    assert not any('"wait_mode": "second"' in (c["body"] or "") for c in transport.calls)
    assert not any("second" in (c["body"] or "") for c in transport.calls if "/emailer_steps" in c["url"])


# ── outreach.enroll ───────────────────────────────────────────────────────────
def test_enroll_carries_sender_and_active_flag(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("outreach.enroll", {"contact_ids": [CONTACT_ID]}, envelope=object())
    assert r.ok
    call = next(c for c in transport.calls if c["method"] == "POST" and "add_contact_ids" in c["url"])
    body = json.loads(call["body"])
    assert body["send_email_from_email_account_id"] == SENDER_ID
    assert body["sequence_active"] is True
    assert body["contact_ids"] == [CONTACT_ID]


# ── outreach.sequence.activate — advertised NON-AUTOMATABLE, never fakes success ─
def test_activate_is_advertised_non_automatable(transport, resolver):
    a = _adapter(transport, resolver)
    cap = _cap(a, "outreach.sequence.activate")
    assert cap.automatable is False
    assert cap.human_required is True
    assert cap.execution_strategy == "provider_ui"


def test_activate_execute_refuses_and_does_not_claim_success(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("outreach.sequence.activate", {"sequence_id": SEQUENCE_ID}, envelope=object())
    assert r.ok is False
    assert r.data["human_required"] is True
    assert r.data["execution_strategy"] == "provider_ui"


# ── outreach.observe / observe() ──────────────────────────────────────────────
def test_observe_capability_parses_status(transport, resolver):
    r = _adapter(transport, resolver).execute("outreach.observe", {"contact_id": CONTACT_ID}, None)
    assert r.ok and r.data["status"] == "active"


def test_observe_method_parses_status(transport, resolver):
    obs = _adapter(transport, resolver).observe(CONTACT_ID)
    assert obs.found and obs.data["status"] == "active"


# ── invariants ────────────────────────────────────────────────────────────────
def test_write_without_envelope_refused(transport, resolver):
    r = _adapter(transport, resolver).execute("contact.upsert", {"email": "t@example.com"}, envelope=None)
    assert r.ok is False and "envelope" in r.error.lower()


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("outreach.delete", {}, envelope=object())


def test_key_never_in_any_result(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("contact.upsert", {"email": "t@example.com"}, envelope=object())
    assert KEY not in f"{r.provider_object_id}{r.error}{dict(r.data)}"


# ── conformance ───────────────────────────────────────────────────────────────
def test_apollo_passes_conformance(transport, resolver):
    probe = ConformanceProbe(write_capability="contact.upsert",
                             write_request={"email": "t@example.com"}, observe_ref=CONTACT_ID)
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]


# ── enrichment / research (reads — no envelope required) ──────────────────────
def test_contact_enrich_returns_the_person_record(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "contact.enrich", {"email": "tasha@nutrients.tech"}, envelope=None)   # read ⇒ no envelope
    assert r.ok and r.provider_object_id == "APOLLO_P1"
    assert r.data.get("title") == "Head of Platform"


def test_company_enrich_by_domain(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "company.enrich", {"domain": "nutrients.tech"}, envelope=None)
    assert r.ok and r.provider_object_id == "APOLLO_O1"
    assert r.data.get("industry") == "food & beverage"


def test_enrich_capabilities_are_reads():
    a = _adapter(FakeTransport(), InMemorySecretResolver())
    for name in ("contact.enrich", "company.enrich"):
        assert _cap(a, name).write is False          # reads ⇒ no execution envelope needed


def test_company_enrich_requires_a_domain(transport, resolver):
    r = _adapter(transport, resolver).execute("company.enrich", {}, envelope=None)
    assert not r.ok and "domain" in r.error.lower()


def test_a_clean_no_match_is_an_empty_success_not_an_error(resolver):
    t = FakeTransport().route("POST", "/people/match", Response(200, json=_load("no_match.json")))
    r = _adapter(t, resolver).execute("contact.enrich", {"email": "nobody@nowhere.tld"}, envelope=None)
    assert r.ok and r.data == {}                     # honest empty, not a failure


def test_enrich_sends_the_key_but_never_leaks_it(transport, resolver):
    r = _adapter(transport, resolver).execute("contact.enrich", {"email": "a@b.com"}, envelope=None)
    sent = [c for c in transport.calls if "/people/match" in c["url"]]
    assert sent and sent[0]["headers"].get("X-Api-Key") == KEY     # key on the wire
    assert KEY not in f"{r.provider_object_id}{r.error}{dict(r.data)}"
