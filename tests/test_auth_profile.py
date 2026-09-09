"""Deployment auth profiles + the Connect-button descriptor derived from a setup guide."""
from __future__ import annotations

import json

import pytest

from redevops_connectors import (
    AuthType,
    ProviderAuthProfile,
    ProviderConnectDescriptor,
    connect_descriptor,
)


def test_default_profile_is_hosted_redevops():
    d = connect_descriptor("google_calendar")
    assert d.auth_profile is ProviderAuthProfile.HOSTED_REDEVOPS


def test_all_four_profiles_exist_with_stable_values():
    assert {p.value for p in ProviderAuthProfile} == {
        "hosted_redevops", "redevops_brokered", "byo_oauth_app", "manual_credential",
    }


def test_google_provider_is_oauth2_with_scopes_and_human_gates():
    d = connect_descriptor("google_calendar")
    assert d.auth_type is AuthType.OAUTH2
    assert d.scopes  # non-empty OAuth scopes
    assert d.human_gates  # app-registration is human-gated
    assert d.embedded_signup is False
    assert d.client_id_ref  # opaque ref, never an inline secret
    assert "secret" not in d.client_id_ref.lower()


def test_whatsapp_sets_embedded_signup():
    d = connect_descriptor("whatsapp_business")
    assert d.embedded_signup is True
    # embedded signup uses Meta's in-page widget, not a loopback/hosted redirect
    assert d.redirect_uri == ""


def test_pure_api_key_provider_has_no_scopes_or_human_gates():
    for provider in ("klaviyo", "polar"):
        d = connect_descriptor(provider)
        assert d.auth_type is AuthType.API_KEY
        assert d.scopes == ()
        assert d.human_gates == ()


def test_manual_profile_carries_no_oauth_client_ref():
    d = connect_descriptor("google_calendar", auth_profile=ProviderAuthProfile.MANUAL_CREDENTIAL)
    assert d.auth_profile is ProviderAuthProfile.MANUAL_CREDENTIAL
    assert d.client_id_ref == ""
    assert d.redirect_uri == ""


def test_byo_and_hosted_resolve_different_client_refs():
    hosted = connect_descriptor("slack", auth_profile=ProviderAuthProfile.HOSTED_REDEVOPS)
    byo = connect_descriptor("slack", auth_profile=ProviderAuthProfile.BYO_OAUTH_APP)
    assert hosted.client_id_ref != byo.client_id_ref
    assert hosted.redirect_uri != byo.redirect_uri  # hosted callback vs loopback


def test_to_dict_round_trips_with_enum_values_and_is_json_serializable():
    d = connect_descriptor("google_calendar")
    raw = d.to_dict()
    assert raw["auth_profile"] == "hosted_redevops"
    assert raw["auth_type"] == "oauth2"
    assert isinstance(raw["scopes"], list) and isinstance(raw["human_gates"], list)
    # every value is a JSON primitive (no bare enums)
    text = json.dumps(raw)
    assert json.loads(text) == raw


def test_descriptor_is_frozen():
    d = connect_descriptor("slack")
    assert isinstance(d, ProviderConnectDescriptor)
    with pytest.raises(Exception):
        d.provider = "other"  # type: ignore[misc]


def test_unknown_provider_raises_keyerror():
    with pytest.raises(KeyError):
        connect_descriptor("not_a_provider")
