# redevops-connectors

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
    slack.py       chat + approvals (OAuth2 v2)
    klaviyo.py     email/SMS marketing (private API key)
    ayrshare.py    publish to many venues in one call (API key)
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

- **Slack** (OAuth2 v2) — the approval surface in the demo. `chat.message.send` (tier 3),
  `chat.message.read`, `approval.request` (tier 3), `identity.read`.
- **Klaviyo** (private API key, JSON:API + `revision` header) — email/SMS marketing.
  `contact.upsert` (tier 2; a duplicate resolves to the existing profile, so it's idempotent),
  `email.event.track` (tier 3; fires a flow — Klaviyo returns 202 with no id, so a tracked event
  is honestly UNKNOWN for reconciliation, never faked).
- **Ayrshare** (bearer API key) — **publish one piece of content to many venues in one call**
  (X, LinkedIn, Instagram, Facebook, TikTok, YouTube, Reddit, Telegram, Threads, Bluesky, …).
  `content.publish` (tier 3) returns an Ayrshare post id + per-venue results; `content.status`.

For multi-venue publishing, **Ayrshare** is the easiest unified API that still delivers the
capability; **Blotato** (flat pricing, native MCP) and open-source, self-hostable **Postiz** are
the natural next adapters — all fit this same contract.

Licensed AGPL-3.0-or-later.
