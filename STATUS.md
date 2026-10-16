# Status

| | |
|---|---|
| Maturity | alpha (reference SDK; fixture-tested) |
| Version | 0.1.0 (`pyproject.toml`); tag `v0.1.0` |
| Last git activity | 2026-10-13 (`git log -1 --format=%cs`; clone is shallow) |
| Activity | active: 37 commits between 2026-07-15 and 2026-10-15 (GitHub API) |
| Distribution | not on PyPI; install from git or a local clone |

## Supported

- The `IntegrationAdapter` contract, `BaseAdapter`, `FakeTransport` / `UrllibTransport`, `OAuthFlow`, `CredentialRef` / `SecretResolver`, and `run_conformance()`.
- 14 provider adapters, exercised against canned fixtures in `fixtures/`: Gmail, Google Calendar, HubSpot, Stripe, Polar, Slack, WhatsApp Cloud API, WhatsApp (WAHA), Klaviyo, Postiz, Ayrshare, Blotato, Apollo, Web.

## Not supported / not established

- No live external call is made by this repo's tests; live behaviour against real providers is not validated here.
- No per-provider install extras: the core and all adapters use only the Python standard library (`dependencies = []`; the only extra is `dev`).
- No governance logic: authority, envelopes and verification live in the Runtime, not in this package.

## Known issues

- The GitHub description says "Providers install on demand"; packaging has no per-provider extras (see above).
- Licence: see `LICENSE.md`.

Related repositories and what each owns: see the OVERLAP section of [README.md](README.md#overlap).
