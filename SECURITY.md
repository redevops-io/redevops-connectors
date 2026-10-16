# Security policy

Please do not open a public issue for a vulnerability.

- Report by email to **redevops@redevops.io** (the ReDevOps organisation contact).

Scope notes: this package handles credential references and OAuth flows. Adapters hold a `CredentialRef`, not a secret, and resolve material only at call time. If you find a code path that logs or stores a secret, treat it as a vulnerability and report it as above.
