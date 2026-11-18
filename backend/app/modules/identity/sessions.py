"""Session cookie, request principal and CSRF (09 "Sign-in and session design").

Cookie `__Host-codehover_session`: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain. Plain-HTTP local
development (SESSION_COOKIE_SECURE=false) uses a distinct, explicitly insecure name, so a production
browser can never send or accept the development cookie. Only the token's SHA-256 is stored.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.core.errors import ApiError
from app.core.security import csrf_token, digest, matches_digest
from app.core.settings import Settings
from app.core.tenancy import set_context

SECURE_COOKIE = "__Host-codehover_session"
DEV_COOKIE = "codehover_session_dev"
# Binds an OAuth flow to the browser that started it (login CSRF protection); lives only for the flow.
SECURE_FLOW_COOKIE = "__Host-codehover_oauth"
DEV_FLOW_COOKIE = "codehover_oauth_dev"
FLOW_COOKIE_SECONDS = 600
ABSOLUTE_LIFETIME = timedelta(days=30)
IDLE_TIMEOUT = timedelta(days=7)
TOUCH_INTERVAL = timedelta(minutes=5)
REAUTH_WINDOW = timedelta(minutes=10)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def cookie_name(settings: Settings) -> str:
    return SECURE_COOKIE if settings.session_cookie_secure else DEV_COOKIE


def flow_cookie_name(settings: Settings) -> str:
    return SECURE_FLOW_COOKIE if settings.session_cookie_secure else DEV_FLOW_COOKIE


def set_flow_cookie(response: Response, settings: Settings, binding: str) -> None:
    response.set_cookie(
        flow_cookie_name(settings),
        binding,
        max_age=FLOW_COOKIE_SECONDS,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",  # sent on the provider's top-level redirect back to the callback
    )


def clear_flow_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        flow_cookie_name(settings),
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        cookie_name(settings),
        token,
        max_age=int(ABSOLUTE_LIFETIME.total_seconds()),
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        cookie_name(settings), path="/", secure=settings.session_cookie_secure, httponly=True, samesite="lax"
    )


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    session_id: UUID
    session_token: str
    csrf_hash: bytes
    reauthenticated_at: datetime

    def recently_authenticated(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) - self.reauthenticated_at <= REAUTH_WINDOW


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One AsyncSession per request (04). Handlers commit explicitly; uncommitted work rolls back."""
    async with request.app.state.sessions() as db:
        yield db


Db = Annotated[AsyncSession, Depends(get_db)]

_LOOKUP = text(
    "SELECT session_id, user_id, csrf_hash, last_seen_at, reauthenticated_at"
    " FROM app_private.session_by_token(:token_hash)"
)


async def optional_principal(request: Request, db: Db) -> Principal | None:
    settings: Settings = request.app.state.settings
    token = request.cookies.get(cookie_name(settings))
    if not token or len(token) > 256:
        return None
    row = (await db.execute(_LOOKUP, {"token_hash": digest(token)})).one_or_none()
    if row is None:
        return None
    now = datetime.now(UTC)
    if now - row.last_seen_at > IDLE_TIMEOUT:
        return None
    if now - row.last_seen_at > TOUCH_INTERVAL:
        await set_context(db, user_id=row.user_id)
        await db.execute(
            text("UPDATE sessions SET last_seen_at = now() WHERE id = :id"), {"id": row.session_id}
        )
        await db.commit()
    await ratelimit.hit(request.app.state.sessions, settings, "user", str(row.user_id))
    await set_context(db, user_id=row.user_id)  # user scope for the rest of the request transaction
    return Principal(row.user_id, row.session_id, token, row.csrf_hash, row.reauthenticated_at)


OptionalPrincipal = Annotated[Principal | None, Depends(optional_principal)]


async def require_principal(principal: OptionalPrincipal) -> Principal:
    if principal is None:
        raise ApiError(401, "UNAUTHENTICATED", "Sign in to continue.")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


async def require_csrf(request: Request, principal: CurrentPrincipal) -> Principal:
    """Unsafe methods: exact Origin match plus the session-bound X-CSRF-Token (09)."""
    settings: Settings = request.app.state.settings
    if request.method in UNSAFE_METHODS:
        if request.headers.get("origin") != settings.public_origin:
            raise ApiError(403, "ORIGIN_REJECTED", "Request origin is not allowed.")
        if not matches_digest(request.headers.get("x-csrf-token"), principal.csrf_hash):
            raise ApiError(403, "CSRF_REJECTED", "Refresh the page and try again.")
    return principal


CsrfPrincipal = Annotated[Principal, Depends(require_csrf)]


def csrf_for(settings: Settings, principal: Principal) -> str:
    return csrf_token(settings.session_secret.get_secret_value(), principal.session_token)
