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
    slack.py       first reference adapter (OAuth2 v2)
fixtures/slack/    canned provider responses for fixture-replay tests
tests/             deterministic — FakeTransport + InMemorySecretResolver
```

## Develop

```
uv run --extra dev python -m pytest -q
```

## First provider

**Slack** (OAuth2 v2) — the approval surface in the Integration Plane demo and the simplest OAuth
to stand up. Capabilities: `chat.message.send` (tier 3, write), `chat.message.read`,
`identity.read`. More providers follow the same shape.

Licensed AGPL-3.0-or-later.
