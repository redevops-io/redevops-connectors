# redevops-connectors

[![License: AGPL-3.0 + Commons Clause](https://img.shields.io/badge/License-AGPL--3.0%20%2B%20Commons%20Clause-blue.svg)](LICENSE) [![NVIDIA Inception](https://img.shields.io/badge/NVIDIA-Inception%20Program%20Member-76B900.svg)](https://www.nvidia.com/en-us/startups/)

> **🚀 NVIDIA Inception Program Member** — ReDevOps is a member of the NVIDIA Inception Program, supporting startups advancing AI and accelerated computing. Membership provides access to NVIDIA technology, technical resources, and the startup ecosystem. It does not imply product endorsement by NVIDIA.

The **ReDevOps Integration Plane connector SDK** — one `IntegrationAdapter` contract, many
governed provider adapters. An adapter translates a logical Runtime operation
(`chat.message.send`, `email.message.read`, …) to and from a provider's API. It does **not**
make governance decisions: the Runtime's authority, execution envelope and verification wrap
every call. The *reasoning* about which capabilities exist and how a request compiles lives in
the Runtime (the Connect Compiler manifest + wizard); this package is the **execution** half.

> Not to be confused with `redevops-integrations`, which is a framework/hyperscaler
> **benchmark** harness. This is the live connector SDK.

## Design

- **stdlib-only core.** No third-party dependency in the SDK; a provider pulls its own deps as
  an extra. The base install ships contracts + lightweight definitions.
- **Everything network-facing is behind a `Transport` seam.** Tests use `FakeTransport` with
  canned fixtures — **no network, no real credentials** — and go live by swapping in
  `UrllibTransport`. An adapter's logic (auth headers, request shape, error normalization) is
  therefore fully deterministic in CI.
- **Credentials are opaque references, resolved only at the moment of use.** An adapter holds a
  `CredentialRef`, not a secret, and calls a `SecretResolver` (broker-backed in the Runtime) to
  get material at the point of the call. Nothing secret is stored on the adapter or logged; the
  conformance suite checks a token never appears in a result.

## The contract

```python
class IntegrationAdapter(Protocol):
    provider: str
    def capabilities(self) -> tuple[Capability, ...]: ...
    def connect(self, config, credential_ref) -> ConnectionState: ...
    def execute(self, capability, request, envelope) -> ProviderResult: ...
    def observe(self, resource_ref) -> Observation: ...
    def subscribe(self, event_types) -> Subscription: ...
    def health(self) -> ProviderHealth: ...
```

Two rules `BaseAdapter` enforces and `run_conformance` checks: an adapter **advertises only what
it implements** (an unadvertised capability raises `UnsupportedCapability`), and a **write
capability refuses without an execution envelope** (it does not validate the envelope — that is
the membrane's job upstream — it requires its presence).

## OAuth — the one-click connect

`OAuthFlow` runs the OAuth2 authorization-code flow: `authorize_url(state)` builds the consent
link the user clicks; `exchange_code(code)` trades it for a `TokenGrant`. The client secret is
resolved from a `CredentialRef` at the token call and never inlined. The flow is transport-
injected, so it is exercised against a fixture token response with no live IdP.

## Going live (what a real connection needs from you)

No code changes — two swaps:

1. Pass `UrllibTransport()` instead of `FakeTransport`.
2. Register a real OAuth app at the provider and put its `client_secret` (for the flow) and the
   resulting `access_token` (for calls) behind `CredentialRef`s in your resolver / the Runtime's
   CredentialBroker. Set the app's redirect URI to your callback.

Until those exist, the adapters run only against fixtures. **No live external call happens in this
repo's tests.**

## Layout

```
redevops_connectors/
  adapter.py       IntegrationAdapter contract + BaseAdapter + result/state types
  transport.py     Transport seam — FakeTransport (tests) · UrllibTransport (live)
  credentials.py   CredentialRef + SecretResolver (broker-backed in the Runtime)
  oauth.py         OAuth2 authorization-code flow
  conformance.py   run_conformance() — the deterministic adapter gate
  providers/
    google_common.py  shared Google OAuth2 helper (Gmail + Calendar)
    gmail.py       send/read email (OAuth2)
    gcalendar.py   calendar events (OAuth2; provider="google_calendar")
    hubspot.py     CRM contacts/companies/deals (OAuth2)
    stripe.py      billing / refunds (API key)
    polar.py       merchant-of-record billing / refunds (API key + organization_id)
    slack.py       chat + approvals (OAuth2 v2)
    whatsapp.py    WhatsApp Business — Meta official Cloud API (token + phone_number_id)
    klaviyo.py     email/SMS marketing (private API key)
    postiz.py      self-hostable multi-venue social publishing (base_url + API key)
    ayrshare.py    publish to many venues in one call (API key)
    blotato.py     multi-venue social publishing, flat pricing (API key)
fixtures/<provider>/  canned provider responses for fixture-replay tests
tests/             deterministic — FakeTransport + InMemorySecretResolver
```

## Develop

```
uv run --extra dev python -m pytest -q
```

## Providers

Same shape for every adapter (injected transport + resolver, capabilities with a tier + a
write flag, envelope-required writes, error normalization, conformance). Auth is per-provider —
OAuth2 or a private API key; the SDK handles both.

**CRM & billing**

- **HubSpot** (OAuth2) — CRM. Contacts/companies/deals lookup + upsert; the CRM system of record in the demo.
- **Stripe** (API key) — billing. Charge/customer lookup and **refunds** (tier 3) — governed, envelope-required.
- **Polar** (API key + non-secret `organization_id`) — merchant-of-record billing; charge lookup and refunds (tier 3).

**Messaging & email**

- **Gmail** (OAuth2) — send/read email.
- **Google Calendar** (OAuth2; `provider="google_calendar"`) — calendar events. Shares `google_common.py`'s OAuth helper with Gmail.
- **Slack** (OAuth2 v2) — the approval surface in the demo. `chat.message.send` (tier 3),
  `chat.message.read`, `approval.request` (tier 3), `identity.read`.
- **WhatsApp Business** (token auth) — Meta's **official** WhatsApp Business Cloud API (Graph). `chat.message.send`; the `phone_number_id` is non-secret config (not a credential). It deliberately does **not** drive any unofficial WhatsApp-Web / scraping path.

**Marketing & social publishing**

- **Klaviyo** (private API key, JSON:API + `revision` header) — email/SMS marketing.
  `contact.upsert` (tier 2; a duplicate resolves to the existing profile, so it's idempotent),
  `email.event.track` (tier 3; fires a flow — Klaviyo returns 202 with no id, so a tracked event
  is honestly UNKNOWN for reconciliation, never faked).
- **Ayrshare** (bearer API key) — **publish one piece of content to many venues in one call**
  (X, LinkedIn, Instagram, Facebook, TikTok, YouTube, Reddit, Telegram, Threads, Bluesky, …).
  `content.publish` (tier 3) returns an Ayrshare post id + per-venue results; `content.status`.
- **Postiz** (self-host `base_url` + API key) — open-source, self-hostable multi-venue social publishing.
- **Blotato** (API key) — multi-venue social publishing with flat pricing.

All three social publishers fit the same contract; pick by hosting/pricing preference (Ayrshare = easiest unified API, Postiz = self-hostable OSS, Blotato = flat pricing).

Licensed AGPL-3.0-or-later.
