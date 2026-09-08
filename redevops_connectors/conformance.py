"""The conformance suite — the contract every adapter must pass, deterministically.

These checks hold for any provider against fixtures (no live calls): an adapter
advertises only what it implements, refuses a consequential action without an envelope,
succeeds with one, never lets credential material ride out in a result, is idempotent
over a repeated call, and can re-observe what it created. Live provider tests are
separate; this is the deterministic gate.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Tuple

from .adapter import BaseAdapter, UnsupportedCapability
from .credentials import InMemorySecretResolver


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ConformanceReport:
    provider: str
    checks: Tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def failures(self) -> Tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)


@dataclass
class ConformanceProbe:
    """What the suite needs to exercise one adapter: a write capability + a sample
    request, a resource to observe, and the name of a capability the adapter must NOT
    claim."""

    write_capability: str
    write_request: Dict[str, Any]
    observe_ref: str
    unadvertised_capability: str = "definitely.not.a.capability"


def _contains_secret(value: Any, secrets: Tuple[str, ...]) -> bool:
    blob = _json.dumps(value, default=str)
    return any(s and s in blob for s in secrets)


def run_conformance(
    adapter: BaseAdapter, *, probe: ConformanceProbe, resolver: InMemorySecretResolver,
) -> ConformanceReport:
    checks: List[Check] = []
    secrets = resolver.known_material()

    # 1 — advertises only what it implements
    try:
        adapter.execute(probe.unadvertised_capability, {}, envelope=object())
        checks.append(Check("advertises_only_implemented", False,
                            "an unadvertised capability did not raise"))
    except UnsupportedCapability:
        checks.append(Check("advertises_only_implemented", True))
    except Exception as e:  # noqa: BLE001
        checks.append(Check("advertises_only_implemented", False, f"raised {type(e).__name__}, not UnsupportedCapability"))

    # 2 — every advertised capability carries a valid tier
    caps = adapter.capabilities()
    bad = [c.name for c in caps if not 0 <= c.tier <= 4]
    checks.append(Check("capabilities_have_valid_tiers", not bad, f"bad tiers: {bad}" if bad else ""))

    # 3 — a consequential (write) capability refuses without an envelope
    r = adapter.execute(probe.write_capability, probe.write_request, envelope=None)
    checks.append(Check("consequential_requires_envelope", (not r.ok) and "envelope" in r.error.lower(),
                        r.error or "did not refuse"))

    # 4 — and succeeds with one
    ok = adapter.execute(probe.write_capability, probe.write_request, envelope=object())
    checks.append(Check("write_succeeds_with_envelope", ok.ok and bool(ok.provider_object_id),
                        ok.error or ""))

    # 5 — no credential material in the result
    leaked = _contains_secret(
        {"data": dict(ok.data), "error": ok.error, "id": ok.provider_object_id}, secrets)
    checks.append(Check("no_credentials_in_result", not leaked,
                        "credential material found in result" if leaked else ""))

    # 6 — idempotent over a repeated identical call (same provider object id)
    again = adapter.execute(probe.write_capability, probe.write_request, envelope=object())
    checks.append(Check("idempotent_repeat", again.provider_object_id == ok.provider_object_id,
                        f"{ok.provider_object_id!r} != {again.provider_object_id!r}"))

    # 7 — the created object can be re-observed (provider id round-trips)
    obs = adapter.observe(probe.observe_ref)
    checks.append(Check("observe_roundtrip", obs.found, f"observe({probe.observe_ref!r}) not found"))

    return ConformanceReport(provider=adapter.provider, checks=tuple(checks))
