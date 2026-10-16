# Contributing

Issues and pull requests are welcome on GitHub: <https://github.com/redevops-io/redevops-connectors/issues>.

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

Guidelines taken from how the repo works today:

- Adapters must stay deterministic in tests: use `FakeTransport` and `InMemorySecretResolver` with fixtures under `fixtures/<provider>/`. Tests must make no network calls and use no real credentials.
- A new adapter should pass `run_conformance()` (advertises only what it implements; writes refuse without an execution envelope; no secret appears in a result).
- Never commit credentials. CI runs a secret scan (`.github/workflows/secret-scan.yml`).
- Contributions are accepted under the repository's licence terms (see `LICENSE.md`).

Security reports: see [SECURITY.md](SECURITY.md).
