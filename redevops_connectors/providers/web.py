"""Web — fetch a page and run a web search, as governed research capabilities.

The research half of the enrichment loop (paired with the Apollo enrichment capabilities): a mission
can pull a public page or run a search, but only through a governed capability whose output is
egress-classified on the way back to an external agent. Both are **reads** (write=False, no execution
envelope); the external reach is what the Runtime governs.

Capabilities:
  * ``web.fetch`` (tier 1, read) — GET a URL and return its text (truncated). No credential — the
    public web; the value is that the reach is a governed, audited capability, not a raw client.
  * ``web.search`` (tier 1, read) — query a search API (default: the Brave Search API,
    ``X-Subscription-Token`` header) and return ``[{title, url, description}]``. Self-hostable /
    swappable via ``search_base``.

``web.fetch`` is governed by a :class:`~redevops_connectors.url_policy.UrlPolicy` (secure by default: http(s)
only, no credentials in the URL, private/loopback/link-local/metadata addresses refused after DNS resolution,
redirects not followed, size/time limits, request-header allowlist). Pass ``fetch_policy=UrlPolicy(...)`` to opt
in to e.g. an allowed-host list. With a ``UrllibTransport`` the fetch connects to the checked address; with any
other transport the URL is validated (including resolution) and then delegated, with NO address pin and with
redirect/proxy handling left to that transport; use ``GuardedTransport`` to keep the pin with a custom policy.

Going live: pass a ``UrllibTransport``; for ``web.search`` put the search key behind a ``CredentialRef``
(material ``{"api_key": "…"}``). ``web.fetch`` needs no credential. Tested against fixtures.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote, urlencode

from ..adapter import (
    BaseAdapter,
    Capability,
    ConnectionState,
    Observation,
    ProviderHealth,
    ProviderResult,
)
from ..transport import Response, Transport, RedirectNotFollowed, TransportTimeout, UrllibTransport
from ..url_policy import (FetchError, GuardedTransport, UrlPolicy, UrlPolicyError, filter_headers, safe_get,
                          validate_url)
from ..credentials import CredentialRef, SecretResolver

BRAVE_SEARCH_BASE = "https://api.search.brave.com/res/v1"
_MAX_CONTENT = 20_000


class WebAdapter(BaseAdapter):
    provider = "web"

    def __init__(self, *, transport: Transport, resolver: SecretResolver,
                 credential_ref: CredentialRef = "", search_base: Optional[str] = None,
                 fetch_policy: Optional[UrlPolicy] = None) -> None:
        super().__init__(transport=transport, resolver=resolver, credential_ref=credential_ref)
        self._fetch_policy = fetch_policy or UrlPolicy()
        self._search_base = (search_base or BRAVE_SEARCH_BASE).rstrip("/")

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("web.fetch", tier=1, write=False),
            Capability("web.search", tier=1, write=False),
        )

    # ── connection / health (stateless; web.fetch needs no account) ──────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        if config.get("search_base"):
            self._search_base = str(config["search_base"]).rstrip("/")
        return ConnectionState(provider=self.provider, connected=True, detail="stateless")

    def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="ok")

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "web.fetch":
            url = str(request.get("url", ""))
            if not url.startswith(("http://", "https://")):
                return ProviderResult(ok=False, capability=capability.name,
                                      error="an http(s) url is required", retryable=False)
            pol = self._fetch_policy
            try:
                hdrs = filter_headers({"User-Agent": "redevops-web/1.0", "Accept": "text/html,*/*"}, pol)
                if isinstance(self._transport, UrllibTransport):
                    resp, _final = safe_get(url, policy=pol, headers=hdrs)
                elif isinstance(self._transport, GuardedTransport):
                    resp = self._transport.request("GET", url, headers=hdrs, timeout=pol.timeout_s)
                else:
                    resp = self._transport.request("GET", validate_url(url, pol), headers=hdrs, timeout=pol.timeout_s)
            except RedirectNotFollowed as e:
                return ProviderResult(ok=False, capability=capability.name, error="redirect not followed",
                                      data={"status": e.status, "location": e.location}, retryable=False)
            except (UrlPolicyError, UnicodeError) as e:
                return ProviderResult(ok=False, capability=capability.name,
                                      error=f"url not permitted: {e}", retryable=False)
            except FetchError as e:
                return ProviderResult(ok=False, capability=capability.name, error=str(e), retryable=e.retryable)
            except TransportTimeout:
                return ProviderResult(ok=False, capability=capability.name, error="timeout", retryable=True)
            if 300 <= resp.status < 400:
                return ProviderResult(ok=False, capability=capability.name, error="redirect not followed",
                                      data={"status": resp.status}, retryable=False)
            if resp.status >= 400:
                return ProviderResult(ok=False, capability=capability.name,
                                      error=f"HTTP {resp.status}", retryable=resp.status >= 500)
            content = (resp.text or "")[:_MAX_CONTENT]
            return ProviderResult(ok=True, capability=capability.name, provider_object_id=url,
                                  data={"url": url, "status": resp.status, "content": content,
                                        "truncated": len(resp.text or "") > _MAX_CONTENT})

        if capability.name == "web.search":
            query = str(request.get("query") or request.get("q") or "")
            if not query:
                return ProviderResult(ok=False, capability=capability.name,
                                      error="a query is required", retryable=False)
            count = int(request.get("count", 5))
            qs = urlencode({"q": query, "count": count})
            mat = self._material()
            key = mat.get("api_key") or mat.get("access_token", "")
            try:
                resp = self._transport.request(
                    "GET", f"{self._search_base}/web/search?{qs}",
                    headers={"Accept": "application/json", "X-Subscription-Token": key})
            except TransportTimeout:
                return ProviderResult(ok=False, capability=capability.name, error="timeout", retryable=True)
            if resp.status >= 400:
                return ProviderResult(ok=False, capability=capability.name,
                                      error=f"HTTP {resp.status}", retryable=resp.status == 429 or resp.status >= 500)
            results = _brave_results(resp.json or {})
            return ProviderResult(ok=True, capability=capability.name,
                                  data={"query": query, "results": results})

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── observe — a web read yields evidence, not an owned resource ───────────────
    def observe(self, resource_ref: str) -> Observation:
        return Observation(resource_ref=resource_ref, found=False)


def _brave_results(payload: dict) -> list:
    rows = (((payload or {}).get("web") or {}).get("results")) or []
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append({"title": r.get("title", ""), "url": r.get("url", ""),
                        "description": r.get("description", "")})
    return out
