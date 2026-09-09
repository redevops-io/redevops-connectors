"""Stripe adapter against fixtures — connect, charge lookup, refund execute (+ an
already-refunded card/validation error), observe, the two invariants, and conformance.
No live account, no real key. Stripe requests are form-encoded; responses are JSON."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers.stripe import StripeAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "stripe"
API_KEY = "sk_test_stripe_secret"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("stripe:key", {"api_key": API_KEY})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "/v1/balance", Response(200, json=_load("balance.json")))
    t.route("GET", "/v1/charges/ch_TEST123", Response(200, json=_load("charge.json")))
    t.route("POST", "/v1/refunds", Response(200, json=_load("refund_created.json")))
    t.route("GET", "/v1/refunds/re_TEST456", Response(200, json=_load("refund_get.json")))
    return t


def _adapter(transport, resolver):
    return StripeAdapter(transport=transport, resolver=resolver, credential_ref="stripe:key")


def test_connect_verifies_the_key_via_balance(transport, resolver):
    s = _adapter(transport, resolver).connect({}, "stripe:key")
    assert s.connected and s.detail == "testmode"


def test_health_ok(transport, resolver):
    assert _adapter(transport, resolver).health().healthy


def test_charge_find_returns_the_charge(transport, resolver):
    r = _adapter(transport, resolver).execute("billing.charge.find", {"charge": "ch_TEST123"})
    assert r.ok and r.provider_object_id == "ch_TEST123"
    assert r.data["amount"] == 1099 and r.data["refunded"] is False


def test_refund_execute_returns_the_refund_id(transport, resolver):
    r = _adapter(transport, resolver).execute(
        "billing.refund.execute", {"charge": "ch_TEST123"}, envelope=object())
    assert r.ok and r.provider_object_id == "re_TEST456"
    assert r.data["status"] == "succeeded" and r.data["charge"] == "ch_TEST123"


def test_refund_is_form_encoded_not_json(transport, resolver):
    _adapter(transport, resolver).execute(
        "billing.refund.execute", {"charge": "ch_TEST123"}, envelope=object())
    sent = [c for c in transport.calls if c["method"] == "POST" and "/v1/refunds" in c["url"]]
    assert sent
    assert sent[0]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert sent[0]["body"] == "charge=ch_TEST123"  # urlencoded form, not a JSON object


def test_refund_by_payment_intent(transport, resolver):
    _adapter(transport, resolver).execute(
        "billing.refund.execute", {"payment_intent": "pi_TEST789"}, envelope=object())
    sent = [c for c in transport.calls if c["method"] == "POST" and "/v1/refunds" in c["url"]]
    assert sent[0]["body"] == "payment_intent=pi_TEST789"


def test_already_refunded_is_a_named_non_retryable_failure(resolver):
    t = FakeTransport().route("POST", "/v1/refunds",
                              Response(400, json=_load("refund_already_refunded.json")))
    r = _adapter(t, resolver).execute(
        "billing.refund.execute", {"charge": "ch_TEST123"}, envelope=object())
    assert not r.ok and not r.retryable
    assert "already been refunded" in r.error


def test_rate_limit_is_retryable(resolver):
    t = FakeTransport().route("POST", "/v1/refunds",
                              Response(429, json={"error": {"type": "api_error", "message": "slow down"}}))
    r = _adapter(t, resolver).execute(
        "billing.refund.execute", {"charge": "ch_TEST123"}, envelope=object())
    assert not r.ok and r.retryable


def test_timeout_is_retryable(resolver):
    t = FakeTransport()
    t.timeouts = ("/v1/refunds",)
    r = _adapter(t, resolver).execute(
        "billing.refund.execute", {"charge": "ch_TEST123"}, envelope=object())
    assert not r.ok and r.retryable and r.error == "timeout"


def test_observe_roundtrips_a_refund(transport, resolver):
    assert _adapter(transport, resolver).observe("re_TEST456").found


def test_write_refuses_without_envelope(transport, resolver):
    r = _adapter(transport, resolver).execute("billing.refund.execute", {"charge": "ch_TEST123"}, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_secret_key_is_sent_but_never_in_the_result(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("billing.refund.execute", {"charge": "ch_TEST123"}, envelope=object())
    sent = [c for c in transport.calls if "/v1/refunds" in c["url"]]
    assert sent and API_KEY in sent[0]["headers"]["Authorization"]  # key on the wire
    assert API_KEY not in f"{r.provider_object_id}{r.error}{dict(r.data)}"  # never in the result


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("billing.payout.execute", {}, envelope=object())


def test_stripe_passes_conformance(transport, resolver):
    probe = ConformanceProbe(write_capability="billing.refund.execute",
                             write_request={"charge": "ch_TEST123"}, observe_ref="re_TEST456")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]
