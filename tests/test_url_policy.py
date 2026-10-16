"""URL policy for web.fetch: local servers and a mocked resolver only (no external network)."""
from __future__ import annotations

import http.server
import threading
import time

import pytest

from redevops_connectors import FakeTransport, InMemorySecretResolver, Response
from redevops_connectors.providers import WebAdapter
from redevops_connectors.transport import UrllibTransport
from redevops_connectors.url_policy import (FetchError, UrlPolicy, UrlPolicyError, ip_block_reason,
                                            safe_get, validate_url)

PUBLIC = lambda host, port: ["93.184.216.34"]  # noqa: E731


def _pol(**kw):
    kw.setdefault("resolver", PUBLIC)
    return UrlPolicy(**kw)


@pytest.mark.parametrize("url", [
    "file:///etc/hosts", "ftp://example.com/", "http://user:pw@example.com/", "http://example.com#frag",
    "http://example.com\\@evil.test/", "http://0x7f000001/", "http://2130706433/", "http://127.1/",
    "http://example.com:8080/", "http:///path", "http://exa mple.com/",
])
def test_rejects_malformed_or_disallowed_urls(url):
    with pytest.raises(UrlPolicyError):
        validate_url(url, _pol())


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "fd00:ec2::254",
    "64:ff9b::7f00:1", "2002:7f00:1::1",
])
def test_blocks_non_public_addresses(ip):
    assert ip_block_reason(ip) is not None


def test_allows_public_addresses():
    assert ip_block_reason("93.184.216.34") is None
    assert ip_block_reason("2606:2800:220:1:248:1893:25c8:1946") is None


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://[::ffff:10.0.0.1]/",
                                 "http://169.254.169.254/latest/"])
def test_rejects_literal_private_hosts(url):
    with pytest.raises(UrlPolicyError):
        validate_url(url, _pol())


def test_rejects_hostname_resolving_to_private_address():
    with pytest.raises(UrlPolicyError):
        validate_url("http://internal.example/", _pol(resolver=lambda h, p: ["93.184.216.34", "10.0.0.1"]))


def test_metadata_blocked_even_for_private_hosts():
    pol = _pol(private_hosts=frozenset({"meta.example"}), resolver=lambda h, p: ["169.254.169.254"])
    with pytest.raises(UrlPolicyError):
        validate_url("http://meta.example/", pol)


def test_host_allowlist_and_denylist():
    pol = _pol(allowed_hosts=frozenset({"*.example.com", "ok.test"}), denied_hosts=frozenset({"bad.example.com"}))
    validate_url("https://a.example.com/", pol)
    validate_url("https://ok.test/", pol)
    for u in ("https://other.test/", "https://bad.example.com/", "https://example.com.evil.test/"):
        with pytest.raises(UrlPolicyError):
            validate_url(u, pol)


def test_request_headers_are_allowlisted():
    with pytest.raises(UrlPolicyError):
        safe_get("http://127.0.0.1:1/", policy=_pol(), headers={"Authorization": "x"})


class _H(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        _H.seen.append((self.path, dict(self.headers)))
        if self.path == "/redir-away":
            self.send_response(302)
            self.send_header("Location", "http://other.invalid/x")
            self.end_headers()
        elif self.path == "/redir-same":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/big":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * 5000)
        elif self.path == "/slow":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.flush()
            time.sleep(2)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hello")

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _H.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _local(server, **kw):
    return _pol(private_hosts=frozenset({"local.test"}), resolver=lambda h, p: ["127.0.0.1"],
                allowed_ports=frozenset({server.server_port}), **kw)


def test_default_policy_refuses_loopback_server(server):
    pol = _pol(resolver=lambda h, p: ["127.0.0.1"], allowed_ports=frozenset({server.server_port}))
    with pytest.raises(UrlPolicyError):
        safe_get(f"http://local.test:{server.server_port}/", policy=pol)
    assert _H.seen == []


def test_fetches_from_pinned_address_with_original_host(server):
    resp, _ = safe_get(f"http://local.test:{server.server_port}/", policy=_local(server))
    assert resp.status == 200 and resp.text == "hello"
    assert _H.seen[0][1]["Host"].startswith("local.test")


def test_redirects_not_followed_by_default(server):
    with pytest.raises(UrlPolicyError):
        safe_get(f"http://local.test:{server.server_port}/redir-same", policy=_local(server))
    assert [p for p, _ in _H.seen] == ["/redir-same"]


def test_same_host_redirect_followed_when_enabled(server):
    resp, final = safe_get(f"http://local.test:{server.server_port}/redir-same",
                           policy=_local(server, follow_redirects=True))
    assert resp.text == "hello" and final.endswith("/ok")


def test_cross_host_redirect_refused_when_enabled(server):
    with pytest.raises(UrlPolicyError):
        safe_get(f"http://local.test:{server.server_port}/redir-away",
                 policy=_local(server, follow_redirects=True))


def test_response_size_limit(server):
    with pytest.raises(FetchError):
        safe_get(f"http://local.test:{server.server_port}/big", policy=_local(server, max_bytes=1000))


def test_total_time_limit(server):
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:
        safe_get(f"http://local.test:{server.server_port}/slow", policy=_local(server, timeout_s=0.5))
    assert time.monotonic() - t0 < 1.8
    assert ei.type.__name__ in ("TransportTimeout", "FetchError")


def test_adapter_with_live_transport_enforces_policy(server):
    a = WebAdapter(transport=UrllibTransport(), resolver=InMemorySecretResolver())
    r = a.execute("web.fetch", {"url": f"http://127.0.0.1:{server.server_port}/"}, None)
    assert not r.ok and "not permitted" in r.error and r.retryable is False
    assert _H.seen == []


def test_adapter_live_transport_opt_in_fetches(server):
    a = WebAdapter(transport=UrllibTransport(), resolver=InMemorySecretResolver(),
                   fetch_policy=_local(server))
    r = a.execute("web.fetch", {"url": f"http://local.test:{server.server_port}/"}, None)
    assert r.ok and r.data["content"] == "hello"


def test_adapter_validates_before_delegating_to_other_transports():
    t = FakeTransport().route("GET", "169.254.169.254", Response(200, text="data"))
    a = WebAdapter(transport=t, resolver=InMemorySecretResolver())
    r = a.execute("web.fetch", {"url": "http://169.254.169.254/latest/"}, None)
    assert not r.ok and t.calls == []


def test_urllib_transport_does_not_forward_headers_across_hosts(server):
    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=other.serve_forever, daemon=True).start()

    class R(_H):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{other.server_port}/target")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), R)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        resp = UrllibTransport().request("GET", f"http://127.0.0.1:{srv.server_port}/",
                                         headers={"Authorization": "Bearer t"})
        assert resp.status == 302
        assert all(p != "/target" for p, _ in _H.seen)
    finally:
        srv.shutdown(); other.shutdown()
