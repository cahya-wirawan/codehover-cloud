"""Stripe adapter: Checkout, Customer Portal, subscriptions and webhook signatures (11, 06).

A small client over Stripe's REST API with the application's own httpx client, like every other adapter,
so tests replay it with `httpx.MockTransport` and no SDK is needed. Only the calls the product makes are
implemented. Webhook signatures follow Stripe's documented scheme: the `Stripe-Signature` header carries
`t=<unix time>` and one or more `v1=<hex HMAC-SHA256 of "<t>.<raw body>">`; the timestamp must be within
the tolerance, and comparisons are constant-time.

Never log request bodies or responses: they carry customer details.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

API = "https://api.stripe.com/v1"
TOLERANCE_SECONDS = 300


class BillingError(Exception):
    def __init__(self, reason: str, status: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class SignatureError(Exception):
    """The webhook is not from Stripe (missing, malformed, stale or wrong signature)."""


@dataclass(frozen=True)
class SubscriptionState:
    id: str
    customer: str
    status: str
    price_id: str | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    ended_at: datetime | None


def _time(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=UTC) if isinstance(value, int | float) else None


def _subscription(data: dict[str, Any]) -> SubscriptionState:
    items = ((data.get("items") or {}).get("data")) or []
    first = items[0] if items else {}
    price = first.get("price") or {}
    # Newer API versions carry the period on the item; older ones on the subscription.
    start = first.get("current_period_start", data.get("current_period_start"))
    end = first.get("current_period_end", data.get("current_period_end"))
    return SubscriptionState(
        id=str(data["id"]),
        customer=str(data["customer"]),
        status=str(data["status"]),
        price_id=price.get("id") if isinstance(price, dict) else None,
        current_period_start=_time(start),
        current_period_end=_time(end),
        cancel_at_period_end=bool(data.get("cancel_at_period_end")),
        ended_at=_time(data.get("ended_at")),
    )


def _form(values: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's bracketed form encoding for nested parameters."""
    pairs: list[tuple[str, str]] = []
    for key, value in values.items():
        name = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            pairs.extend(_form(value, name))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                pairs.extend(
                    _form(item, f"{name}[{index}]")
                    if isinstance(item, dict)
                    else [(f"{name}[{index}]", str(item))]
                )
        elif isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        elif value is not None:
            pairs.append((name, str(value)))
    return pairs


class StripeBilling:
    def __init__(self, secret_key: str, http: httpx.AsyncClient) -> None:
        self._key = secret_key
        self._http = http

    async def _call(
        self, method: str, path: str, params: dict[str, Any] | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            if method == "GET":
                response = await self._http.get(
                    f"{API}{path}", params=tuple(_form(params or {})), headers=headers, timeout=20
                )
            else:
                response = await self._http.post(
                    f"{API}{path}", data=dict(_form(params or {})), headers=headers, timeout=20
                )
        except httpx.HTTPError as error:
            raise BillingError("network") from error
        if response.status_code >= 400:
            raise BillingError("provider", response.status_code)
        body: dict[str, Any] = response.json()
        return body

    async def create_customer(self, workspace_id: str, idempotency_key: str) -> str:
        data = await self._call(
            "POST", "/customers", {"metadata": {"workspace_id": workspace_id}}, idempotency_key
        )
        return str(data["id"])

    async def create_checkout(
        self,
        *,
        customer: str,
        price_id: str,
        workspace_id: str,
        success_url: str,
        cancel_url: str,
        idempotency_key: str,
    ) -> str:
        data = await self._call(
            "POST",
            "/checkout/sessions",
            {
                "mode": "subscription",
                "customer": customer,
                "client_reference_id": workspace_id,
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": {"workspace_id": workspace_id},
                "subscription_data": {"metadata": {"workspace_id": workspace_id}},
            },
            idempotency_key,
        )
        return str(data["url"])

    async def create_portal(self, *, customer: str, return_url: str) -> str:
        data = await self._call(
            "POST", "/billing_portal/sessions", {"customer": customer, "return_url": return_url}
        )
        return str(data["url"])

    async def subscriptions(self, customer: str) -> list[SubscriptionState]:
        """Every subscription of a customer, whatever its status: the authoritative state (11)."""
        data = await self._call(
            "GET", "/subscriptions", {"customer": customer, "status": "all", "limit": 100}
        )
        return [_subscription(item) for item in data.get("data", [])]

    async def dispute(self, dispute_id: str) -> tuple[str, str | None]:
        """A dispute's current status and charge: the authoritative state, whatever order events came in."""
        data = await self._call("GET", f"/disputes/{dispute_id}")
        charge = data.get("charge")
        return str(data.get("status", "")), charge if isinstance(charge, str) else None

    async def charge_customer(self, charge_id: str) -> str | None:
        """The customer a charge belongs to (disputes name a charge, not a customer)."""
        data = await self._call("GET", f"/charges/{charge_id}")
        customer = data.get("customer")
        return customer if isinstance(customer, str) else None


def verify_webhook(payload: bytes, header: str, secret: str, now: float | None = None) -> dict[str, Any]:
    """Return the event if `header` is a valid Stripe signature of `payload`, else raise SignatureError."""
    timestamp = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None or not signatures:
        raise SignatureError("malformed")
    if abs((now if now is not None else time.time()) - timestamp) > TOLERANCE_SECONDS:
        raise SignatureError("stale")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("mismatch")
    try:
        event: dict[str, Any] = json.loads(payload)
    except json.JSONDecodeError as error:
        raise SignatureError("malformed") from error
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("id"), str)
        or not isinstance(event.get("type"), str)
    ):
        raise SignatureError("malformed")
    return event


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """A Stripe-Signature header for `payload` (tests and the local demo's fake checkout)."""
    t = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"
