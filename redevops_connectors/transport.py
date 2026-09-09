"""The transport seam — the only thing that touches the network.

Every adapter makes its provider calls through a :class:`Transport`. In tests that's a
:class:`FakeTransport` returning canned fixtures (no network, no credentials leave the
process); in production it's :class:`UrllibTransport` (stdlib only). Keeping the seam
here means an adapter's logic — auth headers, request shape, response normalization — is
tested deterministically, and going live is a one-line swap.
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.request
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


@dataclass
class UrllibTransport:
    """The live transport (stdlib ``urllib``). Not exercised in CI."""

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
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                raw = fh.read().decode("utf-8")
                status = fh.status
        except urllib.error.HTTPError as e:  # 4xx/5xx carry a body
            raw = e.read().decode("utf-8") if e.fp else ""
            status = e.code
        except TimeoutError as e:  # noqa: F841
            raise TransportTimeout(url) from None
        parsed: Optional[dict] = None
        try:
            obj = _json.loads(raw)
            parsed = obj if isinstance(obj, dict) else None
        except ValueError:
            parsed = None
        return Response(status=status, text=raw, json=parsed)
