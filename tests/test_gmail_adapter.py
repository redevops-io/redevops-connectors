"""Gmail adapter against fixtures — connect, message send, message read (list), observe,
the two invariants (envelope-required-for-writes, no-credential-leak), error normalization,
and conformance. No live mailbox, no real token."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response, UnsupportedCapability
from redevops_connectors.conformance import ConformanceProbe, run_conformance
from redevops_connectors.oauth import OAuth2Config
from redevops_connectors.providers.gmail import GMAIL_SCOPES, GOOGLE_OAUTH, GmailAdapter

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "gmail"
ACCESS_TOKEN = "ya29.TEST-GOOGLE-ACCESS-TOKEN"

_SEND = {"to": "dana@example.com", "subject": "Quarterly review", "body": "Thursday at 3pm."}


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def resolver():
    r = InMemorySecretResolver()
    r.put("gmail:token", {"access_token": ACCESS_TOKEN})
    return r


@pytest.fixture
def transport():
    t = FakeTransport()
    t.route("GET", "users/me/profile", Response(200, json=_load("profile.json")))
    t.route("POST", "users/me/messages/send", Response(200, json=_load("message_sent.json")))
    # observe (specific id) must be matched before the list route (substring order matters)
    t.route("GET", "users/me/messages/MSG01", Response(200, json=_load("message_get.json")))
    t.route("GET", "users/me/messages", Response(200, json=_load("messages_list.json")))
    return t


def _adapter(transport, resolver):
    return GmailAdapter(transport=transport, resolver=resolver, credential_ref="gmail:token")


def test_google_oauth_shape():
    cfg = GOOGLE_OAUTH(client_id="cid", client_secret_ref="gmail:client",
                       redirect_uri="https://app/redirect", scopes=GMAIL_SCOPES)
    assert isinstance(cfg, OAuth2Config)
    assert cfg.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert cfg.token_url == "https://oauth2.googleapis.com/token"
    assert "https://www.googleapis.com/auth/gmail.send" in cfg.scopes


def test_connect_reports_the_mailbox(transport, resolver):
    s = _adapter(transport, resolver).connect({}, "gmail:token")
    assert s.connected and s.account_ref == "acme.ops@example.com"


def test_send_returns_the_message_id(transport, resolver):
    r = _adapter(transport, resolver).execute("email.message.send", _SEND, envelope=object())
    assert r.ok and r.provider_object_id == "MSG01"
    assert r.data["threadId"] == "THREAD01"


def test_read_lists_matching_messages(transport, resolver):
    r = _adapter(transport, resolver).execute("email.message.read", {"q": "subject:review"}, envelope=None)
    assert r.ok and [m["id"] for m in r.data["messages"]] == ["MSG01", "MSG02"]


def test_observe_roundtrips_the_created_message(transport, resolver):
    assert _adapter(transport, resolver).observe("MSG01").found


def test_write_refuses_without_envelope(transport, resolver):
    r = _adapter(transport, resolver).execute("email.message.send", _SEND, envelope=None)
    assert not r.ok and "envelope" in r.error.lower()


def test_bearer_token_is_sent_but_never_in_the_result(transport, resolver):
    a = _adapter(transport, resolver)
    r = a.execute("email.message.send", _SEND, envelope=object())
    sent = [c for c in transport.calls if "messages/send" in c["url"]]
    assert sent and sent[0]["headers"].get("Authorization") == f"Bearer {ACCESS_TOKEN}"  # token on the wire
    blob = f"{r.provider_object_id}{r.error}{dict(r.data)}"
    assert ACCESS_TOKEN not in blob  # ...but never in the result


def test_unadvertised_capability_raises(transport, resolver):
    with pytest.raises(UnsupportedCapability):
        _adapter(transport, resolver).execute("drive.file.upload", {}, envelope=object())


def test_timeout_normalizes_to_a_retryable_failure(resolver):
    t = FakeTransport(timeouts=("messages/send",))
    r = _adapter(t, resolver).execute("email.message.send", _SEND, envelope=object())
    assert not r.ok and r.retryable and "timeout" in r.error


def test_http_429_normalizes_to_a_retryable_failure(resolver):
    t = FakeTransport().route("POST", "messages/send", Response(429, json={"error": {"code": 429, "message": "Rate Limit Exceeded"}}))
    r = _adapter(t, resolver).execute("email.message.send", _SEND, envelope=object())
    assert not r.ok and r.retryable


def test_google_error_is_named_and_not_retryable(resolver):
    t = FakeTransport().route("POST", "messages/send",
                              Response(403, json={"error": {"code": 403, "message": "Insufficient Permission", "status": "PERMISSION_DENIED"}}))
    r = _adapter(t, resolver).execute("email.message.send", _SEND, envelope=object())
    assert not r.ok and not r.retryable and r.error == "Insufficient Permission"


def test_send_without_assigned_id_leaves_object_id_empty(resolver):
    t = FakeTransport().route("POST", "messages/send", Response(200, json={}))
    r = _adapter(t, resolver).execute("email.message.send", _SEND, envelope=object())
    assert r.ok and r.provider_object_id == ""


def test_gmail_passes_conformance(transport, resolver):
    probe = ConformanceProbe(write_capability="email.message.send",
                             write_request=_SEND, observe_ref="MSG01")
    report = run_conformance(_adapter(transport, resolver), probe=probe, resolver=resolver)
    assert report.passed, [c.name for c in report.failures()]


# ───────────────────────── a caller-supplied Message-ID ─────────────────────────
def _sent_raw(transport) -> str:
    import base64
    call = next(c for c in transport.calls if "messages/send" in c["url"])
    raw = json.loads(call["body"])["raw"]
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()


def test_a_requested_message_id_becomes_the_message_id_header(transport, resolver):
    r = _adapter(transport, resolver).execute("email.message.send", {**_SEND, "message_id": "<req-77@redevops.example>"}, envelope=object())
    assert r.ok and "Message-ID: <req-77@redevops.example>" in _sent_raw(transport)
    assert r.data["requested_message_id"] == "<req-77@redevops.example>" and r.provider_object_id == "MSG01"


def test_angle_brackets_are_added_when_the_caller_leaves_them_out(transport, resolver):
    _adapter(transport, resolver).execute("email.message.send", {**_SEND, "message_id": "req-78@redevops.example"}, envelope=object())
    assert "Message-ID: <req-78@redevops.example>" in _sent_raw(transport)


def test_without_a_message_id_no_header_is_set_and_nothing_else_changes(transport, resolver):
    r = _adapter(transport, resolver).execute("email.message.send", _SEND, envelope=object())
    assert "Message-ID" not in _sent_raw(transport) and "requested_message_id" not in r.data


@pytest.mark.parametrize("bad", ["x@y.com\r\nBcc: attacker@evil.example", "a b@c.com", "no-at-sign", "<a@b>@c.com", "<a@b.com><c@d.com>", "a@b.com\nX: y", "<>", "@x.com", "a@", "x" * 300 + "@y.com",
                                 "<a@b.com> ", "a@b.com; rm -rf /"])
def test_a_message_id_that_could_inject_a_header_is_refused_before_anything_is_sent(transport, resolver, bad):
    r = _adapter(transport, resolver).execute("email.message.send", {**_SEND, "message_id": bad}, envelope=object())
    if bad.strip() == "<a@b.com>":
        assert r.ok                                     # surrounding whitespace is stripped: this one is a valid msg-id
        return
    assert not r.ok and not r.retryable and "msg-id" in r.error
    assert not [c for c in transport.calls if "messages/send" in c["url"]]


def test_the_message_id_validator_accepts_ordinary_ids_and_rejects_the_rest():
    from redevops_connectors.providers.gmail import normalize_message_id as n
    assert n(None) is None and n("") is None
    assert n("abc.123+tag@mail.example.com") == "<abc.123+tag@mail.example.com>" and n("  a@b.com\n") == "<a@b.com>"      # surrounding whitespace is stripped
    for bad in ("a@b c", "a@b\nc", "<a@b>c", "a@b\r\nBcc: x@y.z"):
        with pytest.raises(ValueError):
            n(bad)
