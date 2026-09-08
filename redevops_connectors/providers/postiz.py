"""Postiz — schedule/publish one piece of content to many connected channels.

Postiz (https://postiz.com, open-source: github.com/gitroomhq/postiz-app) is a
self-hostable social-media scheduler. One ``content.publish`` call fans out to any of the
channels the account has connected (X, LinkedIn, Facebook, Instagram, Threads, Bluesky,
YouTube, TikTok, …). In Postiz's UI a connected channel is a *channel*; on the API it is
called an **integration**, so a publish targets one or more integration ids.

Auth is a raw API key in the ``Authorization`` header — no ``Bearer`` prefix (OAuth tokens
issued to Postiz apps start with ``pos_`` and are sent the same way). Every call goes
through the injected transport with the key resolved at the moment of use, so the adapter
is tested against fixtures with no live account and no real key.

Capabilities:
  * ``content.publish`` (tier 3, write) — ``POST /posts`` with
    ``{type, date, shortLink, tags, posts:[{integration:{id}, value:[{content, image}],
    settings}]}``; the response is an array of ``{postId, integration}`` — one per targeted
    integration. The first ``postId`` is returned as the reconcilable handle and all of
    them ride along in ``data``.

There is no ``content.status`` / get-post-by-id in the Postiz public API — a published
post is fire-and-forget, not individually re-fetchable (like a tracked event). We never
fake a post read. ``observe`` instead reconciles a connected **integration** (channel) by
id via ``GET /integrations`` — the only get-by-id the public API offers — so a caller can
verify a target channel is still connected.

Going live: pass a ``UrllibTransport`` and put the API key behind a ``CredentialRef``
(material ``{"api_key": "…"}`` — an ``access_token`` starting ``pos_`` also works).
Because Postiz is commonly SELF-HOSTED, the API base is configurable: pass ``base_url`` to
the constructor, or ``config["base_url"]`` to :meth:`connect`. It defaults to the public
cloud base ``https://api.postiz.com/public/v1/``; for a self-hosted instance use
``https://<your-backend-host>/public/v1``. Nothing here calls Postiz until then.
"""
from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..adapter import (
    BaseAdapter,
    Capability,
    ConnectionState,
    Observation,
    ProviderHealth,
    ProviderResult,
)
from ..transport import Response, TransportTimeout

#: The public (cloud) API base. Self-hosted instances override it (see module docstring).
POSTIZ_PUBLIC_API = "https://api.postiz.com/public/v1/"


def _normalize_base(url: str) -> str:
    """A single trailing slash so paths append cleanly."""
    return url.rstrip("/") + "/"


class PostizAdapter(BaseAdapter):
    provider = "postiz"

    def __init__(self, *, transport, resolver, credential_ref: str = "",
                 base_url: Optional[str] = None) -> None:
        super().__init__(transport=transport, resolver=resolver, credential_ref=credential_ref)
        # Configurable for self-hosting; defaults to the public cloud base.
        self._base = _normalize_base(base_url or POSTIZ_PUBLIC_API)

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("content.publish", tier=3, write=True),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        base = config.get("base_url")  # self-host override at connect time
        if base:
            self._base = _normalize_base(str(base))
        status, data, err = self._get("integrations", credential_ref=credential_ref)
        if err or status >= 400:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or _first_error(data))
        rows = data if isinstance(data, list) else []
        identifiers = tuple(str(r.get("identifier", "")) for r in rows if r.get("identifier"))
        account_ref = ""
        if rows:
            cust = rows[0].get("customer") or {}
            account_ref = str(cust.get("id", ""))
        return ConnectionState(
            provider=self.provider, connected=True, account_ref=account_ref,
            scopes=identifiers, detail=f"{len(rows)} channel(s) connected")

    def health(self) -> ProviderHealth:
        status, data, err = self._get("integrations")
        healthy = not err and status < 400
        return ProviderHealth(healthy=healthy,
                              detail=err or ("ok" if healthy else _first_error(data)))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "content.publish":
            integrations = [str(i) for i in (request.get("integrations")
                                             or request.get("integration_ids") or [])]
            content = str(request.get("content", request.get("post", "")))
            images = _media(request.get("media") or request.get("mediaUrls") or [])

            value: List[Dict[str, Any]] = [{"content": content}]
            if images:
                value[0]["image"] = images
            posts = []
            for iid in integrations:
                item: Dict[str, Any] = {"integration": {"id": iid}, "value": value}
                if request.get("settings"):
                    item["settings"] = dict(request["settings"])
                posts.append(item)

            body: Dict[str, Any] = {
                "type": request.get("type", "now"),          # now | schedule | draft
                "date": request.get("date") or _now_iso(),
                "shortLink": bool(request.get("short_link", False)),
                "tags": list(request.get("tags", [])),
                "posts": posts,
            }
            status, data, err = self._post("posts", body)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status < 400 and isinstance(data, list) and data:
                post_ids = [str(p.get("postId", "")) for p in data if p.get("postId")]
                pid = post_ids[0] if post_ids else ""
                return ProviderResult(
                    ok=bool(pid), capability=capability.name, provider_object_id=pid,
                    data={"postIds": post_ids, "type": body["type"]},
                    error="" if pid else "provider returned no post id")
            return ProviderResult(ok=False, capability=capability.name, error=_first_error(data),
                                  retryable=status == 429 or status >= 500)

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── observe (a published post is not re-fetchable; a channel is) ────────────
    def observe(self, resource_ref: str) -> Observation:
        if not resource_ref:
            return Observation(resource_ref=resource_ref, found=False)
        status, data, err = self._get("integrations")
        rows = data if (not err and status < 400 and isinstance(data, list)) else []
        match = next((r for r in rows if str(r.get("id", "")) == resource_ref), None)
        return Observation(resource_ref=resource_ref, data=match or {}, found=match is not None)

    # ── transport helpers (raw key resolved at use, never logged) ───────────────
    def _auth_header(self, credential_ref: str = "") -> Dict[str, str]:
        mat = self._material(credential_ref)
        key = mat.get("api_key") or mat.get("access_token", "")
        return {"Authorization": key}  # raw key — Postiz uses no "Bearer" prefix

    def _post(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        headers = {"Content-Type": "application/json", **self._auth_header(credential_ref)}
        try:
            resp: Response = self._transport.request(
                "POST", self._base + path, headers=headers,
                body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, None, "timeout"
        return resp.status, _body(resp), ""

    def _get(self, path: str, *, credential_ref: str = ""):
        try:
            resp = self._transport.request("GET", self._base + path,
                                           headers=self._auth_header(credential_ref))
        except TransportTimeout:
            return 0, None, "timeout"
        return resp.status, _body(resp), ""


def _body(resp: Response) -> Any:
    """The parsed body, dict OR list. The transport only surfaces top-level objects in
    ``.json``; Postiz's create-post response is a top-level array, so fall back to parsing
    ``.text`` for the live path."""
    if resp.json is not None:
        return resp.json
    if resp.text:
        try:
            return _json.loads(resp.text)
        except ValueError:
            return None
    return None


def _media(items: Any) -> List[Dict[str, str]]:
    """Normalize media into Postiz ``image`` entries. Accepts already-shaped
    ``{"id","path"}`` dicts or bare url strings (an uploaded asset's path)."""
    out: List[Dict[str, str]] = []
    for it in items or []:
        if isinstance(it, dict):
            out.append({k: str(v) for k, v in it.items() if k in ("id", "path")})
        elif it:
            out.append({"path": str(it)})
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _first_error(data: Any) -> str:
    """Postiz/NestJS errors arrive as ``{"message": str|list, "error": str}``."""
    if isinstance(data, dict):
        msg = data.get("message")
        if isinstance(msg, list) and msg:
            return str(msg[0])
        if msg:
            return str(msg)
        if data.get("error"):
            return str(data["error"])
    return "error"
