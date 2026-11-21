"""Personal access tokens (0035): read-only, one member of one workspace, digest stored only."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.modules.workspaces.access import Member

PREFIX = "chm_"
MAX_TOKENS_PER_MEMBER = 20
TOUCH_INTERVAL = timedelta(hours=1)


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def new_token() -> str:
    return PREFIX + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class TokenPrincipal:
    token_id: UUID
    workspace_id: UUID
    user_id: UUID


async def create(db: AsyncSession, member: Member, name: str, days: int) -> dict[str, Any]:
    if member.support:
        raise ApiError(403, "FORBIDDEN", "Support access cannot create tokens.")
    live = (
        await db.execute(
            text(
                "SELECT count(*) FROM access_tokens WHERE workspace_id = :w AND user_id = :u"
                " AND revoked_at IS NULL AND expires_at > now()"
            ),
            {"w": member.workspace_id, "u": member.user_id},
        )
    ).scalar_one()
    if live >= MAX_TOKENS_PER_MEMBER:
        raise ApiError(409, "TOO_MANY_TOKENS", "Revoke a token before creating another.")
    token = new_token()
    row = (
        await db.execute(
            text(
                "INSERT INTO access_tokens(workspace_id, user_id, name, token_hash, prefix, expires_at)"
                " VALUES (:w, :u, :n, :h, :p, :e)"
                " RETURNING id, name, prefix, scope, created_at, expires_at, last_used_at, revoked_at"
            ),
            {
                "w": member.workspace_id,
                "u": member.user_id,
                "n": name.strip(),
                "h": digest(token),
                "p": token[: len(PREFIX) + 8],
                "e": datetime.now(UTC) + timedelta(days=days),
            },
        )
    ).one()
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata)"
            " VALUES (:w, :u, 'access_token.created', 'access_token', :t, CAST(:m AS jsonb))"
        ),
        {"w": member.workspace_id, "u": member.user_id, "t": row.id, "m": f'{{"days": {int(days)}}}'},
    )
    return {**dict(row._mapping), "token": token}


async def listing(db: AsyncSession, member: Member) -> list[dict[str, Any]]:
    """The caller's own tokens in this workspace, newest first; never the secret."""
    rows = await db.execute(
        text(
            "SELECT id, name, prefix, scope, created_at, expires_at, last_used_at, revoked_at"
            " FROM access_tokens WHERE workspace_id = :w AND user_id = :u"
            " ORDER BY created_at DESC LIMIT 100"
        ),
        {"w": member.workspace_id, "u": member.user_id},
    )
    return [dict(r._mapping) for r in rows]


async def revoke(db: AsyncSession, member: Member, token_id: UUID) -> None:
    """Owners revoke their own tokens; workspace admins may revoke anyone's in the workspace."""
    if member.support:
        raise ApiError(403, "FORBIDDEN", "Support access cannot revoke tokens.")
    row = (
        await db.execute(
            text(
                "UPDATE access_tokens SET revoked_at = COALESCE(revoked_at, now())"
                " WHERE workspace_id = :w AND id = :t AND (user_id = :u OR :admin) RETURNING id"
            ),
            {"w": member.workspace_id, "t": token_id, "u": member.user_id, "admin": member.at_least("admin")},
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Token not found.")
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata)"
            " VALUES (:w, :u, 'access_token.revoked', 'access_token', :t, '{}'::jsonb)"
        ),
        {"w": member.workspace_id, "u": member.user_id, "t": token_id},
    )


async def revoke_for_member(db: AsyncSession, workspace_id: UUID, user_id: UUID) -> None:
    """A member who leaves (or is removed) keeps no working token, even if they return later."""
    await db.execute(
        text(
            "UPDATE access_tokens SET revoked_at = now() WHERE workspace_id = :w AND user_id = :u"
            " AND revoked_at IS NULL"
        ),
        {"w": workspace_id, "u": user_id},
    )


async def resolve(db: AsyncSession, token: str) -> TokenPrincipal | None:
    """The token's workspace and user, when it is live (0035 `app_private.access_token`)."""
    if not token.startswith(PREFIX) or len(token) > 100:
        return None
    row = (
        await db.execute(text("SELECT * FROM app_private.access_token(:h)"), {"h": digest(token)})
    ).one_or_none()
    if row is None or row.scope != "mcp:read":
        return None
    return TokenPrincipal(row.token_id, row.workspace_id, row.user_id)


async def touch(db: AsyncSession, principal: TokenPrincipal) -> None:
    """Record use at most hourly (workspace context set by the caller)."""
    await db.execute(
        text(
            "UPDATE access_tokens SET last_used_at = now() WHERE workspace_id = :w AND id = :t"
            " AND (last_used_at IS NULL OR last_used_at < now() - make_interval(secs => :s))"
        ),
        {"w": principal.workspace_id, "t": principal.token_id, "s": TOUCH_INTERVAL.total_seconds()},
    )
