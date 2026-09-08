"""The Slack adapter against fixtures — connect, execute, observe, and the two invariants
(envelope-required-for-writes, no-credential-leak) plus error normalization."""
from __future__ import annotations

from redevops_connectors import FakeTransport, Response, UnsupportedCapability
from redevops_connectors.providers import SlackAdapter

from conftest import ACCESS_TOKEN, load


def _adapter(transport, resolver):
    return SlackAdapter(transport=transport, resolver=resolver, credential_ref="slack:token")


def test_connect_reports_the_workspace(slack_transport, resolver):
    state = _adapter(slack_transport, resolver).connect({}, "slack:token")
    assert state.connected and state.account_ref == "T123"


def test_send_message_returns_a_provider_object_id(slack_transport, resolver):
    a = _adapter(slack_transport, resolver)
    r = a.execute("chat.message.send", {"channel": "C123", "text": "hi"}, envelope=object())
    assert r.ok and r.provider_object_id == "C123:1699999999.000100"


def test_write_refuses_without_an_envelope(slack_transport, resolver):
    r = _adapter(slack_transport, resolver).execute("chat.message.send", {"channel": "C123", "text": "hi"}, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_unadvertised_capability_raises(slack_transport, resolver):
    import pytest
    with pytest.raises(UnsupportedCapability):
        _adapter(slack_transport, resolver).execute("payroll.run", {}, envelope=object())


def test_bearer_token_is_sent_but_never_in_the_result(slack_transport, resolver):
    a = _adapter(slack_transport, resolver)
    r = a.execute("chat.message.send", {"channel": "C123", "text": "hi"}, envelope=object())
    # the token WAS sent on the wire (auth actually happened)...
    sent = [c for c in slack_transport.calls if "chat.postMessage" in c["url"]]
    assert sent and sent[0]["headers"].get("Authorization") == f"Bearer {ACCESS_TOKEN}"
    # ...but it never rides out in the result
    blob = f"{r.provider_object_id}{r.error}{dict(r.data)}"
    assert ACCESS_TOKEN not in blob


def test_observe_roundtrips_the_created_message(slack_transport, resolver):
    obs = _adapter(slack_transport, resolver).observe("C123:1699999999.000100")
    assert obs.found


def test_timeout_normalizes_to_a_retryable_failure(resolver):
    t = FakeTransport(timeouts=("chat.postMessage",))
    r = _adapter(t, resolver).execute("chat.message.send", {"channel": "C123", "text": "hi"}, envelope=object())
    assert not r.ok and r.retryable and "timeout" in r.error


def test_http_429_normalizes_to_a_retryable_failure(resolver):
    t = FakeTransport().route("POST", "chat.postMessage", Response(429, json={"ok": False, "error": "rate_limited"}))
    r = _adapter(t, resolver).execute("chat.message.send", {"channel": "C123", "text": "hi"}, envelope=object())
    assert not r.ok and r.retryable


def test_slack_error_is_not_retryable(resolver):
    t = FakeTransport().route("POST", "chat.postMessage", Response(200, json={"ok": False, "error": "channel_not_found"}))
    r = _adapter(t, resolver).execute("chat.message.send", {"channel": "CX", "text": "hi"}, envelope=object())
    assert not r.ok and not r.retryable and r.error == "channel_not_found"
