"""The declarative setup guides + verify_setup (the read-only smoke grader)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pytest

from redevops_connectors import (
    SETUP_GUIDES,
    AuthType,
    ProviderSetupGuide,
    SetupState,
    all_guides,
    setup_guide,
    verify_setup,
)

_SHIPPED = {"slack", "klaviyo", "ayrshare", "blotato", "postiz", "gmail",
            "google_calendar", "whatsapp_business", "hubspot", "stripe"}


def test_every_shipped_provider_has_a_guide():
    assert _SHIPPED <= set(SETUP_GUIDES)


def test_guides_are_complete_and_serializable():
    for g in all_guides():
        assert isinstance(g, ProviderSetupGuide)
        assert g.display_name and g.used_for and g.setup_url
        assert g.credential_fields, f"{g.provider} has no credential fields"
        assert isinstance(g.auth_type, AuthType)
        d = g.to_dict()  # a UI renders this
        assert d["provider"] == g.provider and isinstance(d["credential_fields"], list)


def test_secret_and_config_fields_are_distinguished():
    # Postiz base_url and WhatsApp phone_number_id are non-secret config, not credentials
    postiz = setup_guide("postiz")
    base = next(c for c in postiz.credential_fields if c.name == "base_url")
    assert base.secret is False
    wa = setup_guide("whatsapp_business")
    pn = next(c for c in wa.credential_fields if c.name == "phone_number_id")
    assert pn.secret is False and pn.env_var == "WHATSAPP_PHONE_NUMBER_ID"


def test_stripe_is_test_mode_capable_and_slack_smoke_is_read_only():
    assert setup_guide("stripe").test_mode_available is True
    assert setup_guide("slack").smoke_capability == "identity.read"  # never a write


# ── verify_setup against fake adapters ──────────────────────────────────────────
@dataclass
class _State:
    connected: bool
    detail: str = ""
    account_ref: str = ""


@dataclass
class _Cap:
    name: str


@dataclass
class _GoodAdapter:
    provider: str = "slack"

    def connect(self, config: Mapping, credential_ref: str) -> _State:
        return _State(connected=True, detail="ok", account_ref="T123")

    def capabilities(self):
        return (_Cap("chat.message.send"), _Cap("identity.read"))


@dataclass
class _BadAdapter:
    provider: str = "slack"

    def connect(self, config, credential_ref):
        return _State(connected=False, detail="invalid_auth")

    def capabilities(self):
        return ()


@dataclass
class _ThrowingAdapter:
    provider: str = "stripe"

    def connect(self, config, credential_ref):
        raise RuntimeError("network down")

    def capabilities(self):
        return ()


def test_verify_setup_grades_a_good_connection_verified_read():
    r = verify_setup(_GoodAdapter(), credential_ref="slack:token")
    assert r.state is SetupState.VERIFIED_READ and r.account_ref == "T123"
    assert "chat.message.send" in r.capabilities


def test_verify_setup_reports_not_connected_on_bad_credential():
    r = verify_setup(_BadAdapter(), credential_ref="slack:token")
    assert r.state is SetupState.NOT_CONNECTED and "invalid_auth" in r.detail


def test_verify_setup_never_crashes_on_a_throwing_connect():
    r = verify_setup(_ThrowingAdapter())
    assert r.state is SetupState.NOT_CONNECTED and "network down" in r.detail
