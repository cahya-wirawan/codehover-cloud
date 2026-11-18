"""Rate limits shared by every API replica (06 "Initial rate policies", T18 hardening).

Fixed windows counted in PostgreSQL (0019), so limits hold across replicas and restarts; nothing is
counted per process. Each hit is its own short transaction, so a request that later fails still counts.
Buckets are an HMAC of the policy and the subject, keyed by the session secret: client addresses and IDs
never reach the database in the clear. Over the limit: 429 `RATE_LIMITED` with `Retry-After`.

Client addresses come from `X-Forwarded-For` only when the direct peer is a trusted proxy
(`TRUSTED_PROXIES`); the bundled nginx overwrites that header with the address it saw (22).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import metrics
from app.core.errors import ApiError
from app.core.settings import Settings


@dataclass(frozen=True)
class Policy:
    name: str
    limit: int
    window_seconds: int


def policies(settings: Settings) -> dict[str, Policy]:
    return {
        "user": Policy("user", settings.rate_limit_user_per_minute, 60),
        "signin": Policy("signin", settings.rate_limit_signin_per_minute, 60),
        "public": Policy("public", settings.rate_limit_public_per_minute, 60),
        "estimate": Policy("estimate", settings.rate_limit_estimates_per_hour, 3600),
        "acquire": Policy("acquire", settings.rate_limit_acquisitions_per_hour, 3600),
        "build": Policy("build", settings.rate_limit_builds_per_day, 86400),
        "billing": Policy("billing", settings.rate_limit_billing_per_hour, 3600),
    }


_HIT = text(
    "INSERT INTO rate_limits(bucket, window_start, count) VALUES (:b, to_timestamp(:w), 1)"
    " ON CONFLICT (bucket, window_start) DO UPDATE SET count = rate_limits.count + 1 RETURNING count"
)


def bucket(settings: Settings, policy: str, subject: str) -> str:
    key = settings.session_secret.get_secret_value().encode()
    return hmac.new(key, f"rate:{policy}:{subject}".encode(), hashlib.sha256).hexdigest()[:32]


async def hit(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, policy_name: str, subject: str
) -> None:
    policy = policies(settings)[policy_name]
    now = time.time()
    window = int(now // policy.window_seconds) * policy.window_seconds
    async with sessions() as db:
        count = (
            await db.execute(_HIT, {"b": bucket(settings, policy_name, subject), "w": window})
        ).scalar_one()
        await db.commit()
    if count > policy.limit:
        metrics.RATE_LIMITED.labels(policy=policy_name).inc()
        retry_after = max(1, int(window + policy.window_seconds - now))
        raise ApiError(
            429,
            "RATE_LIMITED",
            "Too many requests; try again shortly.",
            retryable=True,
            details={"retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


def client_address(request: Request, settings: Settings) -> str:
    """The rate-limit subject for the client: its address, or its /64 for IPv6 (security review of
    0.5.18: one client controls a whole /64, so per-address IPv6 buckets would be trivial to evade)."""
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    networks = [n.strip() for n in settings.trusted_proxies.split(",") if n.strip()]
    if forwarded and _trusted(peer, networks):
        candidate = forwarded.split(",")[-1].strip()  # the address our own proxy appended (overwrote)
        subject = _subject(candidate)
        if subject is not None:
            return subject
    return _subject(peer) or peer


def _subject(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def _trusted(peer: str, networks: list[str]) -> bool:
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(n, strict=False) for n in networks)
