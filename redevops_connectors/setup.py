"""Provider setup guides — the declarative "what to fetch, and where" contract.

The Integration Plane's setup experience should be a product surface, not a developer
checklist: for each provider a small **declarative descriptor** (never hardcoded prose)
that any surface — a Sidekick setup card, a Projects "Apps" screen, a killer-demo
readiness view — renders the same way. This module owns those descriptors and the setup
state machine, plus a generic ``verify_setup`` that runs an adapter's read-only smoke.

Descriptors are pure data (provider-id strings), so this module imports no provider code
and works whether or not a given adapter package is installed. ``verify_setup`` is generic
over the :class:`~redevops_connectors.adapter.IntegrationAdapter` contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class AuthType(str, Enum):
    OAUTH2 = "oauth2"          # authorization-code flow → access token
    API_KEY = "api_key"        # a private API key / secret key
    TOKEN = "token"            # a long-lived bot/app/system token


class SetupState(str, Enum):
    """The visible lifecycle a provider moves through in the setup UI."""
    NOT_CONNECTED = "NOT_CONNECTED"
    AUTHORIZING = "AUTHORIZING"
    CONNECTED = "CONNECTED"          # credential accepted, account reachable
    VERIFIED_READ = "VERIFIED_READ"  # a read-only smoke succeeded
    VERIFIED_WRITE = "VERIFIED_WRITE"  # a governed safe-write succeeded
    MISSION_READY = "MISSION_READY"  # part of a wired, runnable Mission


@dataclass(frozen=True)
class CredentialField:
    """One secret the user pastes. ``secret`` fields are stored via the broker and never
    echoed; ``config`` (non-secret) fields like a phone-number id are plain."""
    name: str            # the material key the adapter reads (api_key / access_token / …)
    label: str
    secret: bool = True
    example: str = ""
    env_var: str = ""    # developer/runtime fallback env var


@dataclass(frozen=True)
class ProviderSetupGuide:
    """Everything a setup surface needs to guide one provider — declarative, render-anywhere."""
    provider: str
    display_name: str
    auth_type: AuthType
    used_for: str
    setup_url: str
    credential_fields: Tuple[CredentialField, ...] = ()
    required_scopes: Tuple[str, ...] = ()
    optional_scopes: Tuple[str, ...] = ()
    account_prerequisites: Tuple[str, ...] = ()
    test_mode_available: bool = False
    manual_steps: Tuple[str, ...] = ()
    smoke_capability: str = ""            # a read-only capability (or connect) to verify wiring
    safe_write_capability: str = ""       # a low-risk write to offer as the "safe test"
    expected_identity_fields: Tuple[str, ...] = ()  # what connect() should return (team, account…)
    common_errors: Tuple[Tuple[str, str], ...] = ()  # (symptom, fix)
    docs_url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider, "display_name": self.display_name,
            "auth_type": self.auth_type.value, "used_for": self.used_for,
            "setup_url": self.setup_url,
            "credential_fields": [
                {"name": c.name, "label": c.label, "secret": c.secret,
                 "example": c.example, "env_var": c.env_var} for c in self.credential_fields],
            "required_scopes": list(self.required_scopes),
            "optional_scopes": list(self.optional_scopes),
            "account_prerequisites": list(self.account_prerequisites),
            "test_mode_available": self.test_mode_available,
            "manual_steps": list(self.manual_steps),
            "smoke_capability": self.smoke_capability,
            "safe_write_capability": self.safe_write_capability,
            "expected_identity_fields": list(self.expected_identity_fields),
            "common_errors": [{"symptom": s, "fix": f} for s, f in self.common_errors],
            "docs_url": self.docs_url,
        }


def _key(env_var: str, *, name: str = "api_key", label: str = "API key", example: str = "") -> CredentialField:
    return CredentialField(name=name, label=label, secret=True, example=example, env_var=env_var)


SETUP_GUIDES: Dict[str, ProviderSetupGuide] = {
    "slack": ProviderSetupGuide(
        provider="slack", display_name="Slack", auth_type=AuthType.OAUTH2,
        used_for="Team notifications, approval requests, and replies inside workflow threads.",
        setup_url="https://api.slack.com/apps",
        credential_fields=(_key("SLACK_BOT_TOKEN", name="access_token",
                                label="Bot User OAuth Token", example="xoxb-…"),),
        required_scopes=("chat:write", "channels:history", "users:read"),
        account_prerequisites=("Invite the bot to every channel it will post in.",),
        manual_steps=(
            "Open api.slack.com/apps → your app (or Create New App).",
            "OAuth & Permissions → Bot Token Scopes → add chat:write, channels:history, users:read.",
            "Install to Workspace → Allow.",
            "Copy the Bot User OAuth Token (xoxb-…). Keep token rotation OFF (avoid xoxe.).",
        ),
        smoke_capability="identity.read", safe_write_capability="chat.message.send",
        expected_identity_fields=("account_ref (team id)",),
        common_errors=(("invalid_auth", "Token is a rotation token (xoxe.) — use the static xoxb- bot token."),
                       ("not_in_channel", "Invite the bot to the target channel first.")),
    ),
    "klaviyo": ProviderSetupGuide(
        provider="klaviyo", display_name="Klaviyo", auth_type=AuthType.API_KEY,
        used_for="Email/SMS marketing — upsert profiles and fire flows.",
        setup_url="https://www.klaviyo.com/settings/account/api-keys",
        credential_fields=(_key("KLAVIYO_API_KEY", label="Private API Key", example="pk_…"),),
        required_scopes=("Profiles: Full", "Events: Full"),
        manual_steps=("Klaviyo → Settings → API Keys.", "Create Private API Key (Full scope).",
                      "Copy the pk_… key."),
        smoke_capability="", safe_write_capability="contact.upsert",
        expected_identity_fields=("account_ref (account id)",),
    ),
    "ayrshare": ProviderSetupGuide(
        provider="ayrshare", display_name="Ayrshare", auth_type=AuthType.API_KEY,
        used_for="Publish one piece of content to many social venues in a single call.",
        setup_url="https://www.ayrshare.com/",
        credential_fields=(_key("AYRSHARE_API_KEY", label="API Key"),),
        account_prerequisites=("Link the social accounts you want to post to in the Ayrshare dashboard.",),
        manual_steps=("Sign up at ayrshare.com.", "Link your social accounts.",
                      "API Key section → copy the key."),
        safe_write_capability="content.publish", expected_identity_fields=("scopes (linked venues)",),
    ),
    "blotato": ProviderSetupGuide(
        provider="blotato", display_name="Blotato", auth_type=AuthType.API_KEY,
        used_for="Multi-venue social publishing built for agents.",
        setup_url="https://www.blotato.com/",
        credential_fields=(_key("BLOTATO_API_KEY", label="API Key"),),
        account_prerequisites=("Connect your social accounts in Blotato.",),
        manual_steps=("Sign in at blotato.com.", "Connect social accounts.", "Settings/API → copy the API key."),
        safe_write_capability="content.publish",
    ),
    "postiz": ProviderSetupGuide(
        provider="postiz", display_name="Postiz", auth_type=AuthType.API_KEY,
        used_for="Open-source, self-hostable multi-venue publishing.",
        setup_url="https://app.postiz.com/",
        credential_fields=(_key("POSTIZ_API_KEY", label="Public API Key"),
                           CredentialField(name="base_url", label="Base URL (self-host only)",
                                           secret=False, env_var="POSTIZ_BASE_URL",
                                           example="https://<host>/public/v1")),
        account_prerequisites=("Connect your channels in Postiz.",),
        manual_steps=("Postiz → Settings → Public API → generate a key.",
                      "Self-hosted: also set the base URL to https://<host>/public/v1."),
        safe_write_capability="content.publish",
    ),
    "gmail": ProviderSetupGuide(
        provider="gmail", display_name="Gmail", auth_type=AuthType.OAUTH2,
        used_for="Send and read email.",
        setup_url="https://developers.google.com/oauthplayground",
        credential_fields=(_key("GOOGLE_ACCESS_TOKEN", name="access_token",
                                label="Google OAuth access token", example="ya29.…"),),
        required_scopes=("https://www.googleapis.com/auth/gmail.send",
                         "https://www.googleapis.com/auth/gmail.readonly"),
        test_mode_available=False,
        manual_steps=("For testing: OAuth Playground → select gmail.send + gmail.readonly → "
                      "Authorize → Exchange for tokens → copy the access token (~1h).",
                      "For production: create an OAuth client in Google Cloud Console + enable the Gmail API."),
        smoke_capability="email.message.read", safe_write_capability="email.message.send",
        common_errors=(("Token expired", "Google access tokens last ~1h — re-mint, or run the OAuth flow for refresh."),),
    ),
    "google_calendar": ProviderSetupGuide(
        provider="google_calendar", display_name="Google Calendar", auth_type=AuthType.OAUTH2,
        used_for="Check availability and create events.",
        setup_url="https://developers.google.com/oauthplayground",
        credential_fields=(_key("GOOGLE_ACCESS_TOKEN", name="access_token",
                                label="Google OAuth access token (same as Gmail)", example="ya29.…"),),
        required_scopes=("https://www.googleapis.com/auth/calendar.events",
                         "https://www.googleapis.com/auth/calendar.readonly"),
        manual_steps=("Same Google token as Gmail; include the calendar scopes.",
                      "Enable the Calendar API in Google Cloud Console for production."),
        smoke_capability="calendar.availability.read", safe_write_capability="calendar.event.create",
    ),
    "whatsapp_business": ProviderSetupGuide(
        provider="whatsapp_business", display_name="WhatsApp Business", auth_type=AuthType.TOKEN,
        used_for="Send WhatsApp messages via the official Meta Cloud API.",
        setup_url="https://developers.facebook.com/",
        credential_fields=(
            _key("WHATSAPP_ACCESS_TOKEN", name="access_token", label="Access token", example="EAAG…"),
            CredentialField(name="phone_number_id", label="Phone number ID", secret=False,
                            env_var="WHATSAPP_PHONE_NUMBER_ID", example="1234567890"),
        ),
        required_scopes=("whatsapp_business_messaging",),
        account_prerequisites=("Add a recipient test number until the app is live.",),
        test_mode_available=True,
        manual_steps=("developers.facebook.com → app → add the WhatsApp product.",
                      "WhatsApp → API Setup → copy the temporary access token (24h) + the Phone number ID.",
                      "For production: create a System User permanent token."),
        smoke_capability="", safe_write_capability="chat.message.send",
        common_errors=(("Recipient not in allowed list", "Add the number as a test recipient in API Setup."),),
    ),
    "hubspot": ProviderSetupGuide(
        provider="hubspot", display_name="HubSpot", auth_type=AuthType.TOKEN,
        used_for="Customer lookup — upsert contacts and add notes.",
        setup_url="https://app.hubspot.com/",
        credential_fields=(_key("HUBSPOT_ACCESS_TOKEN", name="access_token",
                                label="Private-app token", example="pat-…"),),
        required_scopes=("crm.objects.contacts.read", "crm.objects.contacts.write"),
        optional_scopes=("crm.objects.notes.write",),
        manual_steps=("HubSpot → Settings → Integrations → Private Apps → Create a private app.",
                      "Add crm.objects.contacts read + write scopes.",
                      "Create → copy the Access token (pat-…)."),
        smoke_capability="", safe_write_capability="crm.contact.upsert",
    ),
    "stripe": ProviderSetupGuide(
        provider="stripe", display_name="Stripe", auth_type=AuthType.API_KEY,
        used_for="Find charges and execute approved refunds.",
        setup_url="https://dashboard.stripe.com/apikeys",
        credential_fields=(_key("STRIPE_API_KEY", label="Secret key (test mode)", example="sk_test_…"),),
        test_mode_available=True,
        account_prerequisites=("Make a test charge to refund (Stripe test cards).",),
        manual_steps=("Stripe → turn on Test mode.", "Developers → API keys → copy the Secret key (sk_test_…)."),
        smoke_capability="billing.charge.find", safe_write_capability="",  # refunds are Tier 4 — not a "safe" test
        common_errors=(("Key is live-mode", "Use a test-mode key (sk_test_…) for the demo."),),
    ),
    "polar": ProviderSetupGuide(
        provider="polar", display_name="Polar", auth_type=AuthType.API_KEY,
        used_for="Billing / merchant-of-record — find orders and issue refunds.",
        setup_url="https://polar.sh/settings",
        credential_fields=(_key("POLAR_ACCESS_TOKEN", label="Organization Access Token", example="polar_oat_…"),
                           CredentialField(name="organization_id", label="Organization ID", secret=False,
                                           env_var="POLAR_ORG_ID")),
        required_scopes=("orders:read", "refunds:write"),
        test_mode_available=True,
        manual_steps=("Polar → Settings → create an Organization Access Token.",
                      "Copy the polar_oat_… token; note your Organization ID.",
                      "Use the sandbox (sandbox-api.polar.sh) for testing — refunds on production move real money."),
        smoke_capability="billing.order.find", safe_write_capability="",  # refunds are Tier 4
        common_errors=(("Production token", "Refunds hit real orders — use a sandbox token/env for testing."),),
    ),
    "apollo": ProviderSetupGuide(
        provider="apollo", display_name="Apollo", auth_type=AuthType.API_KEY,
        used_for="Cold outreach via Apollo sequences through a warmed, connected mailbox.",
        setup_url="https://app.apollo.io/",
        credential_fields=(_key("APOLLO_API_KEY", label="API Key"),
                           CredentialField(name="sequence_id", label="Sequence (campaign) ID",
                                           secret=False, env_var="APOLLO_SEQUENCE_ID"),
                           CredentialField(name="sender_account_id", label="Sender email account ID",
                                           secret=False, env_var="APOLLO_SENDER_ACCOUNT_ID")),
        account_prerequisites=("Connect and warm the sending mailbox in Apollo.",
                               "Create the sequence (campaign) you will enroll contacts into.",),
        manual_steps=("Apollo → Settings → API → create an API key.",
                      "Connect a mailbox (Settings → Mailboxes) and note the sender email account ID.",
                      "Create a sequence and note its campaign (sequence) ID.",
                      "Activating a sequence is done by a human toggle in the Apollo UI — the API cannot do it."),
        smoke_capability="outreach.observe", safe_write_capability="",
        common_errors=(("Wait mode must be valid",
                        "emailer step wait_mode must be day/minute/hour"),
                       ("Sequence inactive",
                        "activating a sequence is UI-only in Apollo — a human toggles it")),
    ),
}


def setup_guide(provider: str) -> Optional[ProviderSetupGuide]:
    return SETUP_GUIDES.get(provider)


def all_guides() -> Tuple[ProviderSetupGuide, ...]:
    return tuple(SETUP_GUIDES[p] for p in sorted(SETUP_GUIDES))


@dataclass(frozen=True)
class SetupResult:
    """The outcome of a verify pass — the state to show and why."""
    provider: str
    state: SetupState
    detail: str = ""
    account_ref: str = ""
    capabilities: Tuple[str, ...] = ()


def verify_setup(adapter: Any, *, credential_ref: str = "", config: Optional[Dict[str, Any]] = None) -> SetupResult:
    """Run a provider's read-only smoke: call ``connect`` and grade the result. Never
    writes. Returns ``VERIFIED_READ`` when the account is reachable, else
    ``NOT_CONNECTED`` with the provider's own detail — the safe first step before any
    write is offered."""
    provider = getattr(adapter, "provider", "")
    try:
        state = adapter.connect(dict(config or {}), credential_ref)
    except Exception as e:  # noqa: BLE001 — a bad key/timeout is a not-connected result, not a crash
        return SetupResult(provider=provider, state=SetupState.NOT_CONNECTED, detail=f"{type(e).__name__}: {e}")
    connected = bool(getattr(state, "connected", False))
    if not connected:
        return SetupResult(provider=provider, state=SetupState.NOT_CONNECTED,
                           detail=str(getattr(state, "detail", "not connected")))
    caps = tuple(getattr(c, "name", "") for c in adapter.capabilities())
    return SetupResult(
        provider=provider, state=SetupState.VERIFIED_READ,
        detail=str(getattr(state, "detail", "")),
        account_ref=str(getattr(state, "account_ref", "")), capabilities=caps)
