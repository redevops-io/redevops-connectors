"""Stripe — payments, via a secret key bearer token (no OAuth).

Stripe authenticates every call with a secret key sent as a bearer token
(``Authorization: Bearer sk_…``). Requests are ``application/x-www-form-urlencoded``
(NOT JSON) — Stripe reads form fields — while responses are JSON. Every call goes through
the injected transport with the key resolved at the moment of use, so the adapter is
tested against fixtures with no live account and no real key.

Capabilities:
  * ``billing.charge.find`` (tier 1, read) — look up one charge (``GET /v1/charges/{id}``)
    and report its ``amount`` / ``currency`` / ``refunded`` state.
  * ``billing.refund.execute`` (tier 4, write) — refund a charge or payment intent
    (``POST /v1/refunds`` with a form body ``{charge: ch_…}`` or ``{payment_intent: pi_…}``);
    returns a refund object whose id is ``re_…``, the reconcilable handle re-observed via
    ``GET /v1/refunds/{id}``.

Going live: pass a ``UrllibTransport`` and put a Stripe secret key (``sk_live_…`` or
``sk_test_…``) behind a ``CredentialRef`` (material ``{"api_key": "sk_…"}``). Refunds move
real money — they are high-consequence (tier 4); the Runtime gates them (authority +
envelope + verification), this adapter only executes what it is handed. Nothing here calls
Stripe until the live transport is wired.
"""
from __future__ import annotations

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
from ..transport import Response, TransportTimeout

_API = "https://api.stripe.com/v1/"


class StripeAdapter(BaseAdapter):
    provider = "stripe"

    def capabilities(self) -> Tuple[Capability, ...]:
        return (
            Capability("billing.charge.find", tier=1, write=False),
            Capability("billing.refund.execute", tier=4, write=True),
        )

    # ── connection (a GET /v1/balance both proves the key and is a cheap health probe) ──
    def connect(self, config: Mapping[str, Any], credential_ref: str) -> ConnectionState:
        status, data, err = self._get("balance", credential_ref=credential_ref)
        if err or status >= 400:
            return ConnectionState(provider=self.provider, connected=False,
                                   detail=err or _error(data)[1] or "auth failed")
        return ConnectionState(provider=self.provider, connected=True,
                               detail="livemode" if data.get("livemode") else "testmode")

    def health(self) -> ProviderHealth:
        status, data, err = self._get("balance")
        healthy = not err and status < 400
        return ProviderHealth(healthy=healthy, detail=err or ("ok" if healthy else _error(data)[1]))

    # ── execute ───────────────────────────────────────────────────────────────
    def _do_execute(self, capability: Capability, request: Dict[str, Any],
                    envelope: Optional[object]) -> ProviderResult:
        if capability.name == "billing.charge.find":
            charge_id = str(request.get("charge_id") or request.get("charge") or request.get("id") or "")
            status, data, err = self._get(f"charges/{charge_id}")
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status < 400 and str(data.get("id", "")) == charge_id:
                return ProviderResult(
                    ok=True, capability=capability.name, provider_object_id=str(data.get("id", "")),
                    data={"amount": data.get("amount"), "currency": data.get("currency"),
                          "refunded": data.get("refunded"), "amount_refunded": data.get("amount_refunded"),
                          "status": data.get("status")})
            return ProviderResult(ok=False, capability=capability.name,
                                  error=_error(data)[1] or "charge not found",
                                  retryable=status == 429 or status >= 500)

        if capability.name == "billing.refund.execute":
            params: Dict[str, Any] = {}
            if request.get("charge"):
                params["charge"] = request["charge"]
            elif request.get("payment_intent"):
                params["payment_intent"] = request["payment_intent"]
            if request.get("amount") is not None:
                params["amount"] = request["amount"]
            if request.get("reason"):
                params["reason"] = request["reason"]
            # An Idempotency-Key makes a retried POST safe to replay against a live Stripe.
            extra = {"Idempotency-Key": str(request["idempotency_key"])} if request.get("idempotency_key") else {}
            status, data, err = self._post_form("refunds", params, extra_headers=extra)
            if err:
                return ProviderResult(ok=False, capability=capability.name, error=err, retryable=True)
            if status < 400 and str(data.get("id", "")).startswith("re_"):
                return ProviderResult(
                    ok=True, capability=capability.name, provider_object_id=str(data.get("id", "")),
                    data={"status": data.get("status"), "amount": data.get("amount"),
                          "charge": data.get("charge")})
            # A card/validation error (4xx) is a named, non-retryable failure; only a
            # rate-limit or a Stripe server error is worth retrying.
            return ProviderResult(ok=False, capability=capability.name,
                                  error=_error(data)[1] or "refund failed",
                                  retryable=status == 429 or status >= 500)

        return ProviderResult(ok=False, capability=capability.name, error="unhandled capability")

    # ── observe (re-fetch the refund by its re_… id) ────────────────────────────
    def observe(self, resource_ref: str) -> Observation:
        if not resource_ref:
            return Observation(resource_ref=resource_ref, found=False)
        status, data, err = self._get(f"refunds/{resource_ref}")
        found = not err and status < 400 and str(data.get("id", "")) == resource_ref
        return Observation(resource_ref=resource_ref, data=data if found else {}, found=found)

    # ── transport helpers (secret key resolved at use, never logged) ────────────
    def _auth_header(self, credential_ref: str = "") -> Dict[str, str]:
        mat = self._material(credential_ref)
        key = mat.get("api_key") or mat.get("access_token", "")
        return {"Authorization": f"Bearer {key}"}

    def _post_form(self, path: str, params: Mapping[str, Any], *,
                   credential_ref: str = "", extra_headers: Optional[Mapping[str, str]] = None):
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   **self._auth_header(credential_ref), **(extra_headers or {})}
        body = urllib.parse.urlencode(params).encode("utf-8")
        try:
            resp: Response = self._transport.request("POST", _API + path, headers=headers, body=body)
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""

    def _get(self, path: str, *, credential_ref: str = ""):
        try:
            resp = self._transport.request("GET", _API + path, headers=self._auth_header(credential_ref))
        except TransportTimeout:
            return 0, {}, "timeout"
        return resp.status, (resp.json or {}), ""


def _error(data: Any) -> Tuple[str, str]:
    """Stripe wraps failures as ``{"error": {"type": …, "message": …}}`` — return
    ``(type, message)``; ``("", "")`` when the body is not a Stripe error."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("type", "")), str(err.get("message") or err.get("code") or "error")
    return "", ""
