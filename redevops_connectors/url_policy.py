"""URL policy and guarded fetch for ``web.fetch``.

Secure by default; opt-ins are explicit fields on :class:`UrlPolicy`.

* Scheme allowlist (``http``/``https``), no credentials, fragments, control characters or backslashes in the URL;
  IP literals must be canonical (no decimal/hex/octal forms); default ports only unless listed.
* Optional host allowlist and a denylist (exact names or ``*.suffix`` patterns); the denylist wins.
* The host is resolved once, every returned address is checked, and the connection is pinned to a checked
  address (original ``Host`` and TLS server name are kept). Loopback, private, link-local, CGNAT, multicast,
  reserved, unspecified and cloud-metadata addresses are refused, including IPv4-mapped / 6to4 / Teredo / NAT64
  IPv6 forms. Hosts listed in ``private_hosts`` may resolve to private or loopback ranges; link-local,
  metadata and multicast stay blocked for every host.
* Redirects are not followed by default. With ``follow_redirects`` each hop is re-validated and must stay on
  the same host.
* Response size and total time limits; no content decoding; proxy environment variables are ignored.
* Only allowlisted request headers are sent.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from .transport import Response, TransportTimeout

Resolver = Callable[[str, int], List[str]]

DEFAULT_REQUEST_HEADERS = frozenset({"accept", "accept-language", "user-agent"})

_METADATA_IPS = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200", "192.0.0.192"))
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SITE_LOCAL6 = ipaddress.ip_network("fec0::/10")
_NUMERICISH = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$", re.I)
_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")
_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")


class UrlPolicyError(ValueError):
    """The URL (or a redirect, or the resolved address) is not permitted by the policy. Not retryable."""


class FetchError(Exception):
    """Transport failure or limit exceeded while fetching. ``retryable`` hints whether to try again."""

    def __init__(self, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.retryable = retryable


def default_resolver(host: str, port: int) -> List[str]:
    out: List[str] = []
    for _f, _t, _p, _c, sa in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if sa[0] not in out:
            out.append(sa[0])
    return out


@dataclass(frozen=True)
class UrlPolicy:
    allowed_schemes: FrozenSet[str] = frozenset({"http", "https"})
    allowed_hosts: FrozenSet[str] = frozenset()       # empty = any public host; entries: name or "*.suffix"
    denied_hosts: FrozenSet[str] = frozenset()
    private_hosts: FrozenSet[str] = frozenset()       # hosts allowed to resolve to private/loopback ranges
    allowed_ports: Optional[FrozenSet[int]] = None    # None = 80 and 443 only
    follow_redirects: bool = False
    max_redirects: int = 3
    max_bytes: int = 1_048_576
    timeout_s: float = 15.0
    request_headers: FrozenSet[str] = DEFAULT_REQUEST_HEADERS
    resolver: Resolver = field(default=default_resolver, compare=False)


def _norm_pattern(p: str) -> str:
    p = p.strip().lower().rstrip(".")
    return "*." + normalize_host(p[2:]) if p.startswith("*.") else normalize_host(p)


def _host_matches(host: str, patterns) -> bool:
    for p in patterns:
        p = _norm_pattern(p)
        if p.startswith("*."):
            if host.endswith(p[1:]):
                return True
        elif host == p:
            return True
    return False


def normalize_host(host: str) -> str:
    if not isinstance(host, str) or not host.strip():
        raise UrlPolicyError("empty host")
    h = host.strip()
    if h.startswith("["):
        if not h.endswith("]"):
            raise UrlPolicyError("malformed IPv6 literal")
        inner = h[1:-1]
        if "%" in inner:
            raise UrlPolicyError("IPv6 zone identifiers are refused")
        try:
            return str(ipaddress.IPv6Address(inner))
        except ValueError:
            raise UrlPolicyError("malformed IPv6 literal") from None
    if ":" in h:
        raise UrlPolicyError("port or malformed IPv6 literal in host")
    h = h.lower().rstrip(".")
    if not h:
        raise UrlPolicyError("empty host")
    if re.fullmatch(r"[0-9.]+", h):
        try:
            return str(ipaddress.IPv4Address(h))
        except ValueError:
            raise UrlPolicyError("non-canonical IPv4 literal") from None
    if _NUMERICISH.match(h):
        raise UrlPolicyError("numeric IP encodings are refused")
    try:
        h = h.encode("idna").decode("ascii")
    except UnicodeError:
        raise UrlPolicyError("invalid host name") from None
    if len(h) > 253 or not all(_LABEL.match(x) for x in h.split(".")):
        raise UrlPolicyError("invalid host name")
    return h


def _embedded_v4(ip: ipaddress.IPv6Address):
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def ip_block_reason(ip, allow_private: bool = False) -> Optional[str]:
    """None when ``ip`` may be contacted; otherwise a short reason."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address):
        emb = _embedded_v4(ip)
        if emb is not None:
            r = ip_block_reason(emb, allow_private)
            return f"embedded IPv4 {emb}: {r}" if r else None
        if ip in _SITE_LOCAL6 and not allow_private:
            return "site-local address"
    if ip in _METADATA_IPS:
        return "cloud metadata address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_loopback:
        return None if allow_private else "loopback address"
    if ip.is_reserved:
        return "reserved address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return None if allow_private else "shared (CGNAT) address"
    if ip.is_private:
        return None if allow_private else "private address"
    if not ip.is_global:
        return "non-global address"
    return None


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    target: str
    url: str


def parse_url(url: str, policy: UrlPolicy) -> _Target:
    """Static checks (no network)."""
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise UrlPolicyError("empty or oversized URL")
    if _BAD_URL_CHARS.search(url):
        raise UrlPolicyError("URL contains whitespace, control characters or a backslash")
    sp = urlsplit(url)
    scheme = sp.scheme.lower()
    if scheme not in policy.allowed_schemes or scheme not in ("http", "https"):
        raise UrlPolicyError(f"scheme {sp.scheme!r} is not allowed")
    if "@" in sp.netloc:
        raise UrlPolicyError("credentials in the URL are refused")
    if sp.fragment:
        raise UrlPolicyError("URL fragments are refused")
    if not sp.netloc:
        raise UrlPolicyError("URL has no host")
    netloc = sp.netloc
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            raise UrlPolicyError("malformed IPv6 literal")
        raw_host, rest = netloc[:end + 1], netloc[end + 1:]
    else:
        raw_host, sep, rest = netloc.partition(":")
        rest = sep + rest
    host = normalize_host(raw_host)
    if rest not in ("", ":") and not re.fullmatch(r":[0-9]{1,5}", rest):
        raise UrlPolicyError("invalid port")
    port = int(rest[1:]) if len(rest) > 1 else (443 if scheme == "https" else 80)
    if not 0 < port < 65536:
        raise UrlPolicyError("invalid port")
    if port not in (policy.allowed_ports if policy.allowed_ports is not None else (80, 443)):
        raise UrlPolicyError(f"port {port} is not allowed")
    if _host_matches(host, policy.denied_hosts):
        raise UrlPolicyError(f"host {host!r} is denied")
    if policy.allowed_hosts and not _host_matches(host, policy.allowed_hosts):
        raise UrlPolicyError(f"host {host!r} is not on the allowlist")
    path = sp.path or "/"
    return _Target(scheme, host, port, path + (("?" + sp.query) if sp.query else ""), url)


def resolve_checked(t: _Target, policy: UrlPolicy) -> str:
    """Resolve once, check every address, return the address to connect to."""
    try:
        lit = ipaddress.ip_address(t.host.strip("[]"))
    except ValueError:
        lit = None
    allow_private = _host_matches(t.host, policy.private_hosts) if policy.private_hosts else False
    if lit is not None:
        ips = [str(lit)]
    else:
        try:
            ips = policy.resolver(t.host, t.port)
        except OSError as e:
            raise FetchError(f"dns resolution failed: {e}", retryable=True) from None
    if not ips:
        raise FetchError("no addresses for host", retryable=True)
    for ip in ips:
        reason = ip_block_reason(ip, allow_private)
        if reason:
            raise UrlPolicyError(f"{t.host} resolves to a blocked address ({reason})")
    return ips[0]


def validate_url(url: str, policy: UrlPolicy) -> str:
    """Static + resolution checks. Returns the normalised URL's pinned IP."""
    return resolve_checked(parse_url(url, policy), policy)


def filter_headers(headers: Optional[Mapping[str, str]], policy: UrlPolicy) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        if k.lower() not in policy.request_headers:
            raise UrlPolicyError(f"request header {k!r} is not allowed")
        if re.search(r"[\r\n\x00]", str(v)):
            raise UrlPolicyError(f"request header {k!r} contains control characters")
        out[k] = str(v)
    return out


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _fetch_once(t: _Target, ip: str, headers: Dict[str, str], policy: UrlPolicy,
                deadline: float, ssl_context: Optional[ssl.SSLContext]):
    tmo = max(0.05, deadline - time.monotonic())
    if t.scheme == "https":
        conn: http.client.HTTPConnection = _PinnedHTTPS(
            t.host, t.port, ip, tmo, ssl_context or ssl.create_default_context())
    else:
        conn = _PinnedHTTP(t.host, t.port, ip, tmo)
    expired = threading.Event()

    def _abort():
        expired.set()
        s = conn.sock
        if s is not None:
            for fn in (lambda: s.shutdown(socket.SHUT_RDWR), s.close):
                try:
                    fn()
                except OSError:
                    pass

    timer = None
    try:
        try:
            conn.connect()
        except socket.timeout:
            raise TransportTimeout(t.url) from None
        except (OSError, ssl.SSLError) as e:
            raise FetchError(f"cannot connect: {type(e).__name__}", retryable=True) from None
        timer = threading.Timer(max(0.01, deadline - time.monotonic()), _abort)
        timer.daemon = True
        timer.start()
        try:
            conn.request("GET", t.target, headers={**headers, "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            status = resp.status
            location = resp.getheader("Location", "")
            if resp.getheader("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise FetchError("unexpected Content-Encoding")
            declared = resp.getheader("Content-Length")
            if declared and declared.isdigit() and int(declared) > policy.max_bytes:
                raise FetchError("response exceeds the size limit")
            chunks: List[bytes] = []
            total = 0
            while 300 > status or status >= 400:
                if time.monotonic() > deadline:
                    raise TransportTimeout(t.url)
                chunk = resp.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > policy.max_bytes:
                    raise FetchError("response exceeds the size limit")
                chunks.append(chunk)
            if expired.is_set():
                raise TransportTimeout(t.url)
        except (OSError, http.client.HTTPException) as e:
            if expired.is_set() or isinstance(e, socket.timeout):
                raise TransportTimeout(t.url) from None
            raise FetchError(f"transport failure: {type(e).__name__}", retryable=True) from None
    finally:
        if timer is not None:
            timer.cancel()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return status, location, b"".join(chunks)


def safe_get(url: str, *, policy: UrlPolicy, headers: Optional[Mapping[str, str]] = None,
             ssl_context: Optional[ssl.SSLContext] = None) -> Tuple[Response, str]:
    """GET ``url`` under ``policy``. Returns ``(Response, final_url)``; raises UrlPolicyError / FetchError /
    TransportTimeout."""
    hdrs = filter_headers(headers, policy)
    deadline = time.monotonic() + policy.timeout_s
    t = parse_url(url, policy)
    origin = t.host
    for _hop in range(policy.max_redirects + 1):
        ip = resolve_checked(t, policy)
        status, location, body = _fetch_once(t, ip, hdrs, policy, deadline, ssl_context)
        if status in (301, 302, 303, 307, 308):
            if not policy.follow_redirects:
                raise UrlPolicyError(f"redirects are not followed (HTTP {status})")
            if not location:
                raise FetchError("redirect without Location")
            nxt = parse_url(urljoin(t.url, location), policy)
            if nxt.host != origin:
                raise UrlPolicyError("cross-host redirects are refused")
            if t.scheme == "https" and nxt.scheme != "https":
                raise UrlPolicyError("redirect to a weaker scheme is refused")
            t = nxt
            continue
        text = body.decode("utf-8", errors="replace")
        return Response(status=status, text=text), t.url
    raise UrlPolicyError("too many redirects")
