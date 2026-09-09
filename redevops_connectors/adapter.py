"""The IntegrationAdapter contract — the one surface every provider implements.

    capabilities() -> tuple[Capability, ...]
    connect(config, credential_ref)      -> ConnectionState
    execute(capability, request, envelope) -> ProviderResult
    observe(resource_ref)                -> Observation
    subscribe(event_types)               -> Subscription
    health()                             -> ProviderHealth

An adapter maps logical operations to a provider's API and back. It does NOT decide
whether an action is allowed — the Runtime's authority/envelope/verification wrap the
call. Two rules an adapter must uphold, and the conformance suite checks:

  * advertise only what it implements — an unadvertised capability raises
    :class:`UnsupportedCapability` (an honest surface, never a silent no-op);
  * a consequential (write) capability requires an execution envelope to be present —
    the adapter refuses to act without one (it does not validate it; that is the
    membrane's job, upstream).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple

from .credentials import CredentialRef, SecretResolver
from .transport import Transport


class UnsupportedCapability(NotImplementedError):
    """Raised when a capability is asked of an adapter that does not advertise it."""


@dataclass(frozen=True)
class Capability:
    """One logical operation an adapter advertises, with its risk tier (0-4) and whether
    it is a write (a consequential action that needs an execution envelope).

    The three physical-capability fields let an adapter advertise, BEFORE execution, whether
    the Runtime can actually perform the operation via API or whether it must route to a
    human. They default so every existing adapter is unchanged:

      * ``automatable`` — can the Runtime perform it via API at all?
      * ``human_required`` — does completing it require a human (e.g. a provider-UI toggle)?
      * ``execution_strategy`` — ``"api"`` (default) or ``"provider_ui"`` (a planner routes
        a ``provider_ui`` capability to a human gate instead of calling ``execute``)."""

    name: str
    tier: int = 0
    write: bool = False
    automatable: bool = True          # can the Runtime perform it via API?
    human_required: bool = False      # does completing it require a human?
    execution_strategy: str = "api"   # "api" | "provider_ui"

    def __post_init__(self) -> None:
        if not 0 <= int(self.tier) <= 4:
            raise ValueError(f"tier must be 0..4, got {self.tier!r}")


@dataclass(frozen=True)
class ConnectionState:
    provider: str
    connected: bool
    account_ref: str = ""      # the provider's own account/team id (not a secret)
    scopes: Tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ProviderResult:
    """The normalized outcome of one ``execute``. ``provider_object_id`` is the id the
    provider assigned (a message ts, a record id) — what verification re-observes.
    Never carries credential material."""

    ok: bool
    capability: str
    data: Mapping[str, Any] = field(default_factory=dict)
    provider_object_id: str = ""
    error: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class Observation:
    resource_ref: str
    data: Mapping[str, Any] = field(default_factory=dict)
    found: bool = True


@dataclass(frozen=True)
class ProviderHealth:
    healthy: bool
    detail: str = ""


@dataclass(frozen=True)
class Subscription:
    id: str
    event_types: Tuple[str, ...] = ()
    active: bool = False


class IntegrationAdapter(Protocol):
    provider: str

    def capabilities(self) -> Tuple[Capability, ...]: ...
    def connect(self, config: Mapping[str, Any], credential_ref: CredentialRef) -> ConnectionState: ...
    def execute(self, capability: str, request: Mapping[str, Any], envelope: Optional[object]) -> ProviderResult: ...
    def observe(self, resource_ref: str) -> Observation: ...
    def subscribe(self, event_types: Tuple[str, ...]) -> Subscription: ...
    def health(self) -> ProviderHealth: ...


class BaseAdapter:
    """Common machinery: capability lookup, the advertise-only-what-you-implement and
    envelope-required-for-writes guards, and access to the injected transport + resolver.
    Providers subclass this and implement ``_do_execute`` / ``_do_observe`` / etc."""

    provider: str = "base"

    def __init__(self, *, transport: Transport, resolver: SecretResolver,
                 credential_ref: CredentialRef = "") -> None:
        self._transport = transport
        self._resolver = resolver
        self._credential_ref = credential_ref

    # ── the advertised surface ─────────────────────────────────────────────────
    def capabilities(self) -> Tuple[Capability, ...]:
        raise NotImplementedError

    def _capability(self, name: str) -> Capability:
        for c in self.capabilities():
            if c.name == name:
                return c
        raise UnsupportedCapability(f"{self.provider} does not implement {name!r}")

    def supports(self, name: str) -> bool:
        return any(c.name == name for c in self.capabilities())

    # ── the two guards the conformance suite checks ─────────────────────────────
    def execute(self, capability: str, request: Mapping[str, Any],
                envelope: Optional[object] = None) -> ProviderResult:
        cap = self._capability(capability)  # raises UnsupportedCapability if not advertised
        if cap.write and envelope is None:
            return ProviderResult(
                ok=False, capability=capability,
                error="execution envelope required for a consequential (write) capability",
                retryable=False)
        return self._do_execute(cap, dict(request), envelope)

    # ── provider hooks ──────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        raise NotImplementedError

    def connect(self, config: Mapping[str, Any], credential_ref: CredentialRef) -> ConnectionState:
        raise NotImplementedError

    def observe(self, resource_ref: str) -> Observation:
        raise NotImplementedError

    def subscribe(self, event_types: Tuple[str, ...]) -> Subscription:
        return Subscription(id="", event_types=tuple(event_types), active=False)

    def health(self) -> ProviderHealth:
        raise NotImplementedError

    # ── helper: resolved auth material, at the moment of use ────────────────────
    def _material(self, credential_ref: CredentialRef = "") -> Mapping[str, str]:
        ref = credential_ref or self._credential_ref
        return self._resolver.resolve(ref)
