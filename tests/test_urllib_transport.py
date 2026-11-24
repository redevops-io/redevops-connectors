"""The live transport against LOCAL sockets: what a caller gets when a provider is unreachable, slow, sends bytes that are not UTF-8,
sends too much, or redirects elsewhere. No provider and no outside network is contacted."""
import http.server
import socket
import threading

import pytest

from redevops_connectors import transport as T
from redevops_connectors.transport import NO_RESPONSE_STATUS, RedirectNotFollowed, TransportTimeout, UrllibTransport


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/json":
            body, ctype = b'{"ok": true}', "application/json"
        elif self.path == "/latin1":
            body, ctype = b"caf\xe9", "text/plain"
        elif self.path == "/big":
            body, ctype = b"x" * 4096, "text/plain"
        elif self.path == "/slow":
            import time
            time.sleep(3)
            body, ctype = b"late", "text/plain"
        elif self.path == "/elsewhere":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path == "/teapot":
            self.send_response(418)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"short")
            return
        else:
            body, ctype = b"hello", "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a_json_answer_is_parsed_and_a_client_error_keeps_its_status_and_body(base):
    t = UrllibTransport()
    r = t.request("GET", base + "/json")
    assert (r.status, r.json) == (200, {"ok": True})
    r = t.request("GET", base + "/teapot")
    assert (r.status, r.text, r.json) == (418, "short", None)


def test_a_provider_that_is_not_listening_is_a_retryable_looking_response_not_an_exception():
    r = UrllibTransport().request("GET", f"http://127.0.0.1:{closed_port()}/x", timeout=3)
    assert r.status == NO_RESPONSE_STATUS >= 500 and "no response" in r.text and r.json is None


def test_a_name_that_does_not_resolve_is_the_same_kind_of_answer(monkeypatch):
    def no_dns(*a, **k):
        raise socket.gaierror(8, "nodename nor servname provided")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    r = UrllibTransport().request("GET", "http://provider.invalid/x", timeout=3)
    assert r.status == NO_RESPONSE_STATUS and "gaierror" in r.text


def test_a_slow_provider_is_a_timeout(base):
    with pytest.raises(TransportTimeout):
        UrllibTransport().request("GET", base + "/slow", timeout=0.5)


def test_bytes_that_are_not_utf8_do_not_raise(base):
    r = UrllibTransport().request("GET", base + "/latin1")
    assert r.status == 200 and r.text.startswith("caf") and len(r.text) == 4


def test_a_body_over_the_limit_is_a_failure_and_is_not_returned(base, monkeypatch):
    monkeypatch.setattr(T, "MAX_RESPONSE_BYTES", 1024)
    r = UrllibTransport().request("GET", base + "/big")
    assert r.status == NO_RESPONSE_STATUS and "larger than 1024 bytes" in r.text and "xxxx" not in r.text


def test_a_redirect_to_another_port_of_the_same_host_is_not_followed(base):
    """Another port is another service: the headers of the request (a token, say) must not travel there."""
    with pytest.raises(RedirectNotFollowed) as e:
        UrllibTransport().request("GET", base + "/elsewhere", headers={"Authorization": "Bearer t"})
    assert e.value.status == 302 and e.value.location == "http://127.0.0.1:9/x"


@pytest.mark.parametrize("old, new, stays", [
    ("https://api.example.com/a", "https://api.example.com/b", True),
    ("https://api.example.com/a", "https://api.example.com:443/b", True),
    ("http://api.example.com/a", "https://api.example.com/b", True),             # the upgrade
    ("https://api.example.com/a", "http://api.example.com/b", False),            # a weaker scheme
    ("https://api.example.com/a", "https://api.example.com:8443/b", False),      # another port
    ("http://api.example.com:8080/a", "https://api.example.com/b", False),       # not the plain upgrade
    ("https://api.example.com/a", "https://evil.example.net/b", False),
    ("https://api.example.com/a", "https://API.EXAMPLE.COM/b", True),
    ("https://api.example.com/a", "ftp://api.example.com/b", False),
    ("https://api.example.com/a", "https://api.example.com:notaport/b", False),
])
def test_what_counts_as_the_same_service(old, new, stays):
    assert T.same_service(old, new) is stays


def test_an_adapter_reports_an_unreachable_provider_as_a_retryable_failure():
    """The reason for all of the above: every adapter treats a 5xx as retryable, and none catches a raw urllib exception."""
    from redevops_connectors import InMemorySecretResolver
    from redevops_connectors.providers.web import WebAdapter
    resolver = InMemorySecretResolver()
    ref = resolver.put("search", {"api_key": "k"})
    a = WebAdapter(transport=UrllibTransport(), resolver=resolver, credential_ref=ref, search_base=f"http://127.0.0.1:{closed_port()}")
    out = a.execute("web.search", {"query": "anything"}, None)
    assert out.ok is False and out.retryable is True and out.error == f"HTTP {NO_RESPONSE_STATUS}"
