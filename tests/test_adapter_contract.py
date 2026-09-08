"""The provider-agnostic contract: tier validation and the BaseAdapter guards."""
from __future__ import annotations

import pytest

from redevops_connectors import BaseAdapter, Capability, ProviderResult, UnsupportedCapability
from redevops_connectors import InMemorySecretResolver
from redevops_connectors.transport import FakeTransport


def test_capability_tier_must_be_in_range():
    with pytest.raises(ValueError, match="tier must be 0..4"):
        Capability("x.y", tier=7)


class _Stub(BaseAdapter):
    provider = "stub"

    def capabilities(self):
        return (Capability("thing.write", tier=2, write=True), Capability("thing.read", tier=0))

    def _do_execute(self, capability, request, envelope):
        return ProviderResult(ok=True, capability=capability.name, provider_object_id="obj-1")


def _stub():
    return _Stub(transport=FakeTransport(), resolver=InMemorySecretResolver())


def test_unadvertised_capability_raises():
    with pytest.raises(UnsupportedCapability):
        _stub().execute("thing.delete", {}, envelope=object())


def test_write_requires_envelope():
    r = _stub().execute("thing.write", {}, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_write_succeeds_with_envelope():
    r = _stub().execute("thing.write", {}, envelope=object())
    assert r.ok and r.provider_object_id == "obj-1"


def test_read_needs_no_envelope():
    r = _stub().execute("thing.read", {}, envelope=None)
    assert r.ok
