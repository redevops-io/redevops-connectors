"""Credential handling — opaque references, resolved only at the moment of use.

An adapter never holds raw secret material and never logs it. It carries a
:class:`CredentialRef` (an opaque handle) and calls a :class:`SecretResolver` at the
point of use to obtain the material (an access token, a client secret). In the Runtime
the resolver is backed by the CredentialBroker / SecretRef store; here an
:class:`InMemorySecretResolver` serves tests. :func:`redact` scrubs known material from
anything about to be logged or returned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Protocol

# An opaque handle to secret material held elsewhere (the broker / secret store).
# It is safe to log and to carry in a plan; it is NOT the secret.
CredentialRef = str


class SecretResolver(Protocol):
    """Resolves a :class:`CredentialRef` to its material at the moment of use. The
    material must never be stored on the adapter or written to a log/receipt."""

    def resolve(self, ref: CredentialRef) -> Mapping[str, str]: ...


@dataclass
class InMemorySecretResolver:
    """A resolver for tests and single-process use. Real deployments inject a
    broker-backed resolver instead."""

    _store: Dict[CredentialRef, Dict[str, str]] = field(default_factory=dict)

    def put(self, ref: CredentialRef, material: Mapping[str, str]) -> CredentialRef:
        self._store[ref] = dict(material)
        return ref

    def resolve(self, ref: CredentialRef) -> Mapping[str, str]:
        if ref not in self._store:
            raise KeyError(f"no credential material for ref {ref!r}")
        return dict(self._store[ref])

    def known_material(self) -> tuple[str, ...]:
        """Every secret string currently held — used by the conformance no-leak check."""
        return tuple(v for mat in self._store.values() for v in mat.values() if v)


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Replace any known secret substring with a stable placeholder so it can't ride out
    in a log line, error, or result."""
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "«redacted»")
    return out
