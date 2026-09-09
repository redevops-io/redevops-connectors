"""Polar (polar.sh) — billing / merchant-of-record, via an Organization Access Token.

The same ``billing.*`` capability names as the Stripe adapter, so Polar drops into the same
billing leg of a Mission. Auth is a bearer ``polar_oat_…`` (or ``polar_pat_…``) token; base
URL is production by default and switches to the sandbox via ``sandbox=True`` (ctor) or
``config["sandbox"]``. Every call goes through the injected transport, so it's tested
against fixtures with no live account and no real token.

Capabilities:
  * ``billing.order.find`` (tier 1, read) — ``GET /v1/orders/{id}`` (or the latest order when
    no id is given); returns amount/currency/status/refunded_amount.
  * ``billing.refund.execute`` (tier 4, write) — ``POST /v1/refunds/`` for an order. **Real
    money** on production — the Runtime gates it; the adapter only executes.

Going live: put the token behind a ``CredentialRef`` (material ``{"api_key": "polar_oat_…"}``)
and pass ``organization_id`` (not a secret) via ctor/config where an org scope is needed.
"""
from __future__ import annotations

import json as _json
import urllib.parse
from typing import Any, Dict, Mapping, Optional, Tuple

from ..adapter import (
    BaseAdapter,
    Capability,
    ConnectionState,
    Observation,
    ProviderHealth,
    ProviderResult,
)
from ..credentials import CredentialRef, SecretResolver
from ..transport import Response, Transport, TransportTimeout

_PROD = "https://api.polar.sh"
_SANDBOX = "https://sandbox-api.polar.sh"


class PolarAdapter(BaseAdapter):
    provider = "polar"

    def __init__(self, *, transport: Transport, resolver: SecretResolver,
                 credential_ref: CredentialRef = "", organization_id: str = "",
                 sandbox: bool = False) -> None:
        super().__init__(transport=transport, resolver=resolver, credential_ref=credential_ref)
        self._org = organization_id
        self._base = _SANDBOX if sandbox else _PROD

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("billing.order.find", tier=1, write=False),
            Capability("billing.refund.execute", tier=4, write=True),
        )

    # ── connection ──────────────────────────────────────────────────────────────
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        if config.get("organization_id"):
            self._org = str(config["organization_id"])
        if config.get("sandbox") is not None:
            self._base = _SANDBOX if config["sandbox"] else _PROD
        status, _data, err = self._get("/v1/orders/", {"limit": "1"}, credential_ref=credential_ref)
        connected = not err and status < 400
        return ConnectionState(provider=self.provider, connected=connected,
                               account_ref=self._org, detail=err or ("ok" if connected else "auth failed"))

    def health(self) -> ProviderHealth:
        status, _data, err = self._get("/v1/orders/", {"limit": "1"})
        healthy = not err and status < 400
        return ProviderHealth(healthy=healthy, detail=err or ("ok" if healthy else "error"))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "billing.order.find":
            oid = request.get("id", "")
            if oid:
                status, data, err = self._get(f"/v1/orders/{oid}", {})
            else:
                q = {"limit": "1"}
                if self._org:
                    q["organization_id"] = self._org
                status, data, err = self._get("/v1/orders/", q)
                data = (data.get("items") or [{}])[0] if isinstance(data, dict) else {}
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status >= 400 or not data.get("id"):
                return ProviderResult(ok=False, capability=capability.name,
                                      error=_msg(data) or "order not found", retryable=status >= 500)
            return ProviderResult(ok=True, capability=capability.name, provider_object_id=str(data["id"]),
                                  data={"total_amount": data.get("total_amount"), "currency": data.get("currency"),
                                        "status": data.get("status"), "refunded_amount": data.get("refunded_amount")})

        if capability.name == "billing.refund.execute":
            body: Dict[str, Any] = {
                "order_id": request.get("order_id", ""),
                "reason": request.get("reason", "customer_request"),
                "revoke_benefits": bool(request.get("revoke_benefits", False)),
            }
            if request.get("amount") is not None:
                body["amount"] = int(request["amount"])
            if request.get("comment"):
                body["comment"] = request["comment"]
            status, data, err = self._post("/v1/refunds/", body)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status not in (200, 201) or not data.get("id"):
                return ProviderResult(ok=False, capability=capability.name, error=_msg(data) or "refund failed",
                                      retryable=status == 429 or status >= 500)
            return ProviderResult(ok=True, capability=capability.name, provider_object_id=str(data["id"]),
                                  data={"status": data.get("status"), "amount": data.get("amount")})

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    def observe(self, resource_ref: str) -> Observation:
        status, data, err = self._get(f"/v1/refunds/{resource_ref}", {})
        found = not err and status < 400 and str(data.get("id", "")) == resource_ref
        return Observation(resource_ref=resource_ref, data=data if found else {}, found=found)

    # ── transport helpers (bearer resolved at use, never logged) ────────────────
    def _auth(self, credential_ref: str = "") -> Dict[str, str]:
        mat = self._material(credential_ref)
        return {"Authorization": f"Bearer {mat.get('api_key') or mat.get('access_token', '')}"}

    def _get(self, path: str, query: Mapping[str, Any], *, credential_ref: str = ""):
        url = self._base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        try:
            resp = self._transport.request("GET", url, headers={"Accept": "application/json", **self._auth(credential_ref)})
        except TransportTimeout:
            return 0, {}, "timeout"
        return self._normalize(resp)

    def _post(self, path: str, payload: Mapping[str, Any], *, credential_ref: str = ""):
        headers = {"Content-Type": "application/json", "Accept": "application/json", **self._auth(credential_ref)}
        try:
            resp: Response = self._transport.request("POST", self._base + path, headers=headers,
                                                     body=_json.dumps(payload).encode("utf-8"))
        except TransportTimeout:
            return 0, {}, "timeout"
        return self._normalize(resp)

    @staticmethod
    def _normalize(resp: Response):
        data = resp.json if resp.json is not None else {}
        if resp.status == 429 or resp.status >= 500:
            return resp.status, data, f"http {resp.status}"
        return resp.status, data, ""


def _msg(data: Any) -> str:
    if isinstance(data, dict):
        if isinstance(data.get("error"), str):
            return data["error"]
        det = data.get("detail")
        if isinstance(det, str):
            return det
        if isinstance(det, list) and det:
            return str(det[0].get("msg", "error"))
    return ""
