"""redevops-connectors — the ReDevOps Integration Plane connector SDK.

One contract, many governed provider adapters. An adapter translates logical Runtime
operations (``chat.message.send``, ``email.message.read`` …) to and from a provider's
API; it does **not** make governance decisions — the Runtime's authority, envelope and
verification wrap every call. The SDK core is stdlib-only and network-agnostic: every
outbound call goes through an injected :class:`~redevops_connectors.transport.Transport`,
so adapters are exercised deterministically against fixtures with no live calls and no
real credentials, and go live by swapping in :class:`~redevops_connectors.transport.UrllibTransport`
plus real OAuth app credentials resolved through a :class:`~redevops_connectors.credentials.SecretResolver`.

Credentials are never held in code or logged: an adapter receives an opaque
``CredentialRef`` and resolves the material at the moment of use.
"""
from __future__ import annotations

from .adapter import (
    BaseAdapter,
    Capability,
    ConnectionState,
    IntegrationAdapter,
    Observation,
    ProviderHealth,
    ProviderResult,
    Subscription,
    UnsupportedCapability,
)
from .credentials import CredentialRef, InMemorySecretResolver, SecretResolver, redact
from .oauth import OAuth2Config, OAuthError, OAuthFlow, TokenGrant
from .connect_flow import (
    ConnectResult,
    CredentialBroker,
    InMemoryCredentialBroker,
    LoopbackConnect,
    embedded_signup_exchange,
)
from .transport import FakeTransport, Response, Transport, UrllibTransport
from .conformance import Check, ConformanceReport, run_conformance
from .setup import (
    SETUP_GUIDES,
    AuthType,
    CredentialField,
    ProviderSetupGuide,
    SetupResult,
    SetupState,
    all_guides,
    setup_guide,
    verify_setup,
)

__all__ = [
    # adapter contract
    "IntegrationAdapter",
    "BaseAdapter",
    "Capability",
    "ConnectionState",
    "ProviderResult",
    "Observation",
    "ProviderHealth",
    "Subscription",
    "UnsupportedCapability",
    # credentials
    "CredentialRef",
    "SecretResolver",
    "InMemorySecretResolver",
    "redact",
    # oauth
    "OAuth2Config",
    "OAuthFlow",
    "OAuthError",
    "TokenGrant",
    # connect orchestration (the runtime fetches the token; no secret is ever pasted)
    "LoopbackConnect",
    "ConnectResult",
    "CredentialBroker",
    "InMemoryCredentialBroker",
    "embedded_signup_exchange",
    # transport
    "Transport",
    "Response",
    "FakeTransport",
    "UrllibTransport",
    # conformance
    "run_conformance",
    "ConformanceReport",
    "Check",
    # setup guides (the declarative "what to fetch" contract every setup UI renders)
    "ProviderSetupGuide",
    "CredentialField",
    "AuthType",
    "SetupState",
    "SetupResult",
    "SETUP_GUIDES",
    "setup_guide",
    "all_guides",
    "verify_setup",
]
