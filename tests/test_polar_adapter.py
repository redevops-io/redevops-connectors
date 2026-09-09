"""Polar adapter against fixtures — order lookup, refund (write), observe, invariants,
conformance. No live account, no real token."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers import PolarAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "polar"
TOKEN = "polar_oat_test_secret"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("polar:token", {"api_key": TOKEN})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "/v1/orders/ord_1", Response(200, json=_load("order_get.json")))
    t.route("GET", "/v1/orders/", Response(200, json=_load("orders_list.json")))
    t.route("POST", "/v1/refunds/", Response(201, json=_load("refund_created.json")))
    t.route("GET", "/v1/refunds/ref_1", Response(200, json=_load("refund_get.json")))
    return t


def _adapter(transport, resolver):
    return PolarAdapter(transport=transport, resolver=resolver, credential_ref="polar:token",
                        organization_id="org_1")


def test_connect_ok(transport, resolver):
    assert _adapter(transport, resolver).connect({}, "polar:token").connected is True


def test_order_find_by_id_and_latest(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("billing.order.find", {"id": "ord_1"}, None)
    assert r.ok and r.provider_object_id == "ord_1" and r.data["total_amount"] == 9720
    latest = a.execute("billing.order.find", {}, None)  # no id → latest from the list
    assert latest.ok and latest.provider_object_id == "ord_1"


def test_refund_requires_envelope_then_succeeds(transport, resolver):
    a = _adapter(transport, resolver)
    assert not a.execute("billing.refund.execute", {"order_id": "ord_1"}, envelope=None).ok
    r = a.execute("billing.refund.execute", {"order_id": "ord_1"}, envelope=object())
    assert r.ok and r.provider_object_id == "ref_1"


def test_observe_roundtrips_the_refund(transport, resolver):
    assert _adapter(transport, resolver).observe("ref_1").found


def test_token_never_in_result(transport, resolver):
    r = _adapter(transport, resolver).execute("billing.refund.execute", {"order_id": "ord_1"}, envelope=object())
    assert TOKEN not in f"{r.provider_object_id}{r.error}{dict(r.data)}"


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("billing.payout", {}, envelope=object())


def test_sandbox_base_url_switch(resolver):
    a = PolarAdapter(transport=FakeTransport(), resolver=resolver, credential_ref="polar:token", sandbox=True)
    assert a._base == "https://sandbox-api.polar.sh"


def test_polar_passes_conformance(transport, resolver):
    probe = ConformanceProbe(write_capability="billing.refund.execute",
                             write_request={"order_id": "ord_1"}, observe_ref="ref_1")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]
