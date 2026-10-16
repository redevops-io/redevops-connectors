# redevops-connectors

[![License: AGPL-3.0 + Commons Clause](https://img.shields.io/badge/License-AGPL--3.0%20%2B%20Commons%20Clause-blue.svg)](LICENSE.md) [![NVIDIA Inception](https://img.shields.io/badge/NVIDIA-Inception%20Program%20Member-76B900.svg)](https://www.nvidia.com/en-us/startups/)

> **🚀 NVIDIA Inception Program Member** — ReDevOps is a member of the NVIDIA Inception Program, supporting startups advancing AI and accelerated computing. Membership provides access to NVIDIA technology, technical resources, and the startup ecosystem. It does not imply product endorsement by NVIDIA.

**What it does.** The ReDevOps Integration Plane connector SDK: one `IntegrationAdapter` contract and 14 provider adapters
(CRM, billing, messaging, social publishing, outreach, web research). An adapter translates a logical Runtime operation
(`chat.message.send`, `email.message.read`, ...) to and from a provider's API; it does not make governance decisions.

**Who it's for.** Developers wiring a governed agent runtime to external providers. Its main consumer in this org is
[`agentic-os`](https://github.com/redevops-io/agentic-os) (optional `connectors` and `projects` extras pin this repo at `v0.1.0`;
`agentic_os/integrations/live.py` imports it lazily).

**Maturity: alpha.** Version 0.1.0, one tag (`v0.1.0`). Adapters run only against canned fixtures in this repo; no live
provider call is made by the tests. See [STATUS.md](STATUS.md).

**Ownership, overlap and support status.** Owned by ReDevOps. Status: **active** (last commit 2026-10-13; 37 commits in the
90 days before 2026-10-15, per the GitHub API). Not to be confused with
[`redevops-integrations`](https://github.com/redevops-io/redevops-integrations), a framework/cloud conformance harness. See
[OVERLAP](#overlap) below. Questions and bug reports go to GitHub Issues ([CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md)).

**3-minute example** (only commands that exist in this repo; they are the ones CI runs, plus a single test file):

```bash
git clone https://github.com/redevops-io/redevops-connectors && cd redevops-connectors
python -m pip install -e ".[dev]"            # stdlib-only core + pytest
python -m pytest -q tests/test_slack_adapter.py tests/test_conformance.py   # one adapter + the conformance gate, fixtures only
python -m pytest -q                          # everything (this is what CI runs)
```

There is no runnable live example in this repo: going live needs your own OAuth app / API keys (see "Going live" below).
Locally, the two conformance/adapter test files above passed (11 tests) on 2026-10-15.

**Evidence.** The test suite under [`tests/`](tests) and the canned responses under [`fixtures/`](fixtures); CI is
[`.github/workflows/tests.yml`](.github/workflows/tests.yml). Classification: Internally reproduced (tests are deterministic
and run in CI; no external reproduction).

**Limitations.** Fixture-tested only; no live-provider validation is recorded in this repo. The package is not on PyPI; install from git.

**Install note (verified against `pyproject.toml`).** The core has `dependencies = []` and the only extra is `dev` (pytest).
The adapters import only the Python standard library, so all 14 ship in the base install; there are **no per-provider extras**
today, despite the GitHub description saying "Providers install on demand" and the comment in `pyproject.toml` about
providers pulling their own dependencies. A provider that needs a third-party package would add its own extra.

The **ReDevOps Integration Plane connector SDK** — one `IntegrationAdapter` contract, many
governed provider adapters. An adapter translates a logical Runtime operation
(`chat.message.send`, `email.message.read`, …) to and from a provider's API. It does **not**
make governance decisions: the Runtime's authority, execution envelope and verification wrap
every call. The *reasoning* about which capabilities exist and how a request compiles lives in
the Runtime (the Connect Compiler manifest + wizard); this package is the **execution** half.

> Not to be confused with `redevops-integrations`, which is a framework/hyperscaler
> **conformance** harness. This is the connector SDK (fixture-tested; live use needs your own credentials).

## Design

- **stdlib-only core.** No third-party dependency in the SDK. Today every provider adapter is also stdlib-only, so there are
  no per-provider extras (a future provider needing a third-party package would add one).
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
    whatsapp_waha.py  WhatsApp via WAHA — self-hosted HTTP gateway, multi-number (base_url + key)
    klaviyo.py     email/SMS marketing (private API key)
    postiz.py      self-hostable multi-venue social publishing (base_url + API key)
    ayrshare.py    publish to many venues in one call (API key)
    blotato.py     multi-venue social publishing, flat pricing (API key)
    apollo.py      cold-outreach sequences + contact/company enrichment (API key)
    web.py         web.fetch (a public page, URL-policy guarded) + web.search (research; Brave by default)
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
- **WhatsApp (WAHA)** (self-host `base_url` + optional `X-Api-Key`) — the self-hosted **WAHA** HTTP gateway, for **multiple numbers** paired by QR with no Meta approval (the SMB/LATAM path). `chat.message.send` (tier 3; `to` normalized to a `…@c.us` chatId), `chat.message.read`, `session.status`. A distinct provider from the Cloud-API adapter above; delivery reconciles via WAHA webhooks.

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

**Enrichment, outreach & research**

- **Apollo** (`apollo.io`, private API key in `X-Api-Key`) — cold-outreach sequences through a warmed
  mailbox (`contact.upsert` · `outreach.sequence.configure` · `outreach.enroll` · `outreach.sequence.activate`
  — the last advertised **non-automatable** / human-required, since Apollo activation is UI-only) **and**
  contact/company **enrichment** (`contact.enrich` via `people/match`, `company.enrich` by domain — reads, no
  envelope; the returned PII/customer-content is classified on egress).
- **Web** (`web.fetch` needs no key; `web.search` uses a Brave Search API key by default, swappable via
  `search_base`) — the research half of the loop: `web.fetch` GETs a public page under a URL policy (http(s) only, no URL credentials, private/loopback/link-local/metadata
  addresses refused after DNS resolution and the connection pinned to the checked address, redirects off by default,
  size/time limits, header allowlist; opt-ins such as allowed hosts via `UrlPolicy`; a redirect that is not followed is reported as an error carrying its `Location`, never as success). With `UrllibTransport` or `GuardedTransport` the connection is pinned; with any other transport (e.g. a fake) the URL is only validated, then delegated, and pinning/redirect/proxy behaviour is that transport's, `web.search` returns
  `{title, url, description}`. Both reads; the governed capability is what makes the external reach audited.

All three social publishers fit the same contract; pick by hosting/pricing preference (Ayrshare = easiest unified API, Postiz = self-hostable OSS, Blotato = flat pricing).

Licensed AGPL-3.0-or-later with the Commons Clause (source-available; see [LICENSE.md](LICENSE.md)).

## OVERLAP

Several ReDevOps repositories sound alike. This table is identical in `redevops-connectors`, `redevops-integrations` and `agent-harness`.

| Repo | Owns | Does not own | Licence file (see its own repo) |
|---|---|---|---|
| [`redevops-connectors`](https://github.com/redevops-io/redevops-connectors) | The `IntegrationAdapter` contract, OAuth/credential-reference plumbing, and 14 provider adapters (Gmail, Google Calendar, HubSpot, Stripe, Polar, Slack, WhatsApp Cloud API, WhatsApp via WAHA, Klaviyo, Postiz, Ayrshare, Blotato, Apollo, Web). Tested against canned fixtures only. | Governance decisions, Missions, framework benchmarks. | `LICENSE.md` (AGPL-3.0-or-later with Commons Clause) |
| [`redevops-integrations`](https://github.com/redevops-io/redevops-integrations) | Eight framework/cloud conformance slices (NVIDIA NeMo, PydanticAI, LlamaIndex, CrewAI, Azure, Google, AWS, DigitalOcean) that wrap a real framework agent with the Mission SDK and classify native vs native+ReDevOps results. | The connector adapter contract, the Mission SDK itself. | Apache-2.0 |
| [`agent-harness`](https://github.com/redevops-io/agent-harness) | Single-step agent building blocks: LLM client, tool registry, approval flow, sandbox, guardrails, eval harness. | Mission lifecycle, provider integrations, context planning. | AGPL-3.0 text (`LICENSE`) |
| [`mission-sdk`](https://github.com/redevops-io/mission-sdk) | The `redevops-mission` package and `rdo` CLI: the developer-facing way to author and run governed Missions. | Provider adapters, framework conformance slices. | Apache-2.0 |
| [`agentic-os`](https://github.com/redevops-io/agentic-os) | The control plane and Mission kernel, plus the reference apps; it is the main consumer of `redevops-connectors` (optional `connectors` / `projects` extras). | Connector contract source, SDK packaging. | See that repo |

Common confusions: `redevops-connectors` is the live-adapter SDK, `redevops-integrations` is a conformance harness (not an adapter library), and `agent-harness` is not a Mission runtime. Do not `pip install agent-harness` from PyPI: that name belongs to an unrelated third-party project.
