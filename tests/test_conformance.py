"""The Slack adapter must pass the deterministic conformance suite."""
from __future__ import annotations

from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.providers import SlackAdapter


def test_slack_passes_conformance(slack_transport, resolver):
    adapter = SlackAdapter(transport=slack_transport, resolver=resolver, credential_ref="slack:token")
    probe = ConformanceProbe(
        write_capability="chat.message.send",
        write_request={"channel": "C123", "text": "hi"},
        observe_ref="C123:1699999999.000100",
    )
    report = run_conformance(adapter, probe=probe, resolver=resolver)
    assert report.passed, f"conformance failures: {[c.name for c in report.failures()]}"
    names = {c.name for c in report.checks}
    assert {"advertises_only_implemented", "consequential_requires_envelope",
            "no_credentials_in_result", "idempotent_repeat", "observe_roundtrip"} <= names
