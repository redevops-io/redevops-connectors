"""The transport seam — the only thing that touches the network.

Every adapter makes its provider calls through a :class:`Transport`. In tests that's a
:class:`FakeTransport` returning canned fixtures (no network, no credentials leave the
process); in production it's :class:`UrllibTransport` (stdlib only). Keeping the seam
here means an adapter's logic — auth headers, request shape, response normalization — is
tested deterministically, and going live is a one-line swap.
"""
from __future__ import annotations

import http.client
import json as _json
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Protocol, Tuple


@dataclass(frozen=True)
class Response:
    """A normalized HTTP response. ``json`` is the parsed body when it is JSON."""

    status: int
    text: str = ""
    json: Optional[dict] = None


class Transport(Protocol):
    def request(
        self, method: str, url: str, *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 30.0,
    ) -> Response: ...


class TransportTimeout(Exception):
    """A request exceeded its timeout — normalized by adapters to a retryable failure."""


class RedirectNotFollowed(Exception):
    """A redirect the transport declined to follow (e.g. to another host). Never a success: carries the
    status and the ``Location`` so a caller can decide explicitly."""

    def __init__(self, status: int, location: str = "") -> None:
        super().__init__(f"redirect (HTTP {status}) not followed")
        self.status = status
        self.location = location


@dataclass
class FakeTransport:
    """A deterministic transport for tests. Routes a request to a canned :class:`Response`
    by the first ``(method, url-substring)`` route that matches, records every call (so a
    test can assert the bearer token was actually sent — and that it never appears in a
    result), and can be told to raise a timeout for a route."""

    routes: List[Tuple[str, str, Response]] = field(default_factory=list)
    timeouts: Tuple[str, ...] = ()  # url substrings that raise TransportTimeout
    calls: List[dict] = field(default_factory=list)

    def route(self, method: str, url_substring: str, response: Response) -> "FakeTransport":
        self.routes.append((method.upper(), url_substring, response))
        return self

    def request(self, method, url, *, headers=None, body=None, timeout=30.0) -> Response:
        self.calls.append(
            {"method": method.upper(), "url": url, "headers": dict(headers or {}),
             "body": body.decode("utf-8") if isinstance(body, bytes) else body}
        )
        for sub in self.timeouts:
            if sub in url:
                raise TransportTimeout(url)
        for m, sub, resp in self.routes:
            if m == method.upper() and sub in url:
                return resp
        return Response(status=404, text=f"no fake route for {method} {url}")


def _port(u) -> int:
    try:
        return u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return -1


def same_service(old_url: str, new_url: str) -> bool:
    """Whether a redirect stays with the service the request (and its credentials) was meant for: the same host AND the same port,
    never a weaker scheme. Another port on the same host is another service. The one change of port allowed is the upgrade from
    http on 80 to https on 443."""
    old, new = urlsplit(old_url), urlsplit(new_url)
    if new.scheme not in ("http", "https") or (old.hostname or "").lower() != (new.hostname or "").lower():
        return False
    if old.scheme == "https" and new.scheme != "https":
        return False
    if _port(old) == _port(new):
        return True
    return old.scheme == "http" and new.scheme == "https" and (_port(old), _port(new)) == (80, 443)


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the same service (see :func:`same_service`), so credentials sent in headers are not forwarded
    elsewhere. Otherwise the 3xx is returned to the caller as-is."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not same_service(req.full_url, newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirects)


class _TooLarge(Exception):
    pass


def _read_text(fh) -> str:
    """The body as text: at most MAX_RESPONSE_BYTES, and bytes that are not UTF-8 are replaced, not raised."""
    data = fh.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise _TooLarge()
    return data.decode("utf-8", errors="replace")


#: The status a caller sees when no HTTP response was received at all (connection refused, DNS failure, TLS failure, a reset).
#: Not a status any provider sent: 599 is outside the registered range on purpose, and ``text`` says what happened.
NO_RESPONSE_STATUS = 599
#: The most a response body may be. A provider API answer is small; anything larger is cut off and treated as a failure.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass
class UrllibTransport:
    """The live transport (stdlib ``urllib``). Its failure paths are tested against local sockets (tests/test_urllib_transport.py);
    no provider is contacted in CI.

    A request always ends in a :class:`Response`, a :class:`TransportTimeout` or a :class:`RedirectNotFollowed`. A connection that
    could not be made is a ``Response`` with ``status`` :data:`NO_RESPONSE_STATUS` (adapters already treat a 5xx as a retryable
    failure), never a raw ``urllib`` exception that no adapter catches."""

    #: Sent unless the adapter supplies its own. The stdlib default (``Python-urllib/x``)
    #: is blocked by Cloudflare-fronted APIs (e.g. Polar) with a 403, so give a real one.
    user_agent: str = "redevops-connectors/1.0"

    def request(self, method, url, *, headers=None, body=None, timeout=30.0) -> Response:
        req = urllib.request.Request(url, data=body, method=method.upper())
        sent = {k.lower() for k in (headers or {})}
        if "user-agent" not in sent:
            req.add_header("User-Agent", self.user_agent)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _OPENER.open(req, timeout=timeout) as fh:
                raw = _read_text(fh)
                status = fh.status
        except urllib.error.HTTPError as e:  # 4xx/5xx carry a body
            if e.code in (301, 302, 303, 307, 308):
                raise RedirectNotFollowed(e.code, e.headers.get("Location", "") if e.headers else "") from None
            try:
                raw = _read_text(e) if e.fp else ""
            except _TooLarge:
                raw = ""
            status = e.code
        except _TooLarge:
            return Response(status=NO_RESPONSE_STATUS, text=f"transport failure: the response is larger than {MAX_RESPONSE_BYTES} bytes")
        except TimeoutError:
            raise TransportTimeout(url) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):          # a connect or read timeout arrives wrapped
                raise TransportTimeout(url) from None
            return Response(status=NO_RESPONSE_STATUS, text=f"transport failure: no response ({type(e.reason).__name__ if isinstance(e.reason, BaseException) else 'URLError'})")
        except (OSError, http.client.HTTPException) as e:   # a reset or a malformed response while reading
            return Response(status=NO_RESPONSE_STATUS, text=f"transport failure: no usable response ({type(e).__name__})")
        parsed: Optional[dict] = None
        try:
            obj = _json.loads(raw)
            parsed = obj if isinstance(obj, dict) else None
        except ValueError:
            parsed = None
        return Response(status=status, text=raw, json=parsed)
