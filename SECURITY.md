# Security policy

Please do not open a public issue for a vulnerability.

- GitHub private vulnerability reporting is **not enabled** for this repository (checked 2026-10-15).
- Report by email to **redevops@redevops.io** (the organisation contact listed in the GitHub org metadata). A dedicated security contact, response times and a supported-versions policy: contact to be confirmed.

Scope notes: this package handles credential references and OAuth flows. Adapters hold a `CredentialRef`, not a secret, and resolve material only at call time. If you find a code path that logs or stores a secret, treat it as a vulnerability and report it as above.
