"""Bring your own key (M6, T24; 07 "BYOK P1 stores envelope-encrypted secrets server-side and exposes only
masked metadata").

A workspace owner can store one Anthropic API key. It is checked with the provider before it is saved,
sealed with AES-256-GCM bound to the workspace and provider (the associated data), and shown only as a
hint. Builds that choose `funding: own_key` call the provider with it; their spend is billed by the
provider to the key's account, so it is bounded by the build cap but never uses the plan allowance.
Replacing or removing a key wipes the sealed value; a running build that loses its key stops.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import anthropic
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.models.anthropic import OFFICIAL_BASE_URL
from app.core.errors import ApiError
from app.core.security import Sealer
from app.core.tenancy import set_context
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

logger = logging.getLogger(__name__)
PROVIDERS = ("anthropic",)
Verifier = Callable[[str, str], Awaitable[bool]]  # (provider, key) -> accepted; raises on outages


def associated(workspace_id: UUID, provider: str) -> bytes:
    return f"provider-key:{workspace_id}:{provider}".encode()


def hint(key: str) -> str:
    return f"{key[:7]}…{key[-4:]}" if len(key) > 16 else "…"


async def verify_with_provider(provider: str, key: str) -> bool:
    """One cheap authenticated call through the official SDK to the official API (never the operator's
    `ANTHROPIC_BASE_URL`, 08); False when the key is refused."""
    client = anthropic.AsyncAnthropic(api_key=key, base_url=OFFICIAL_BASE_URL, max_retries=0, timeout=15.0)
    try:
        await client.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        return False
    finally:
        await client.close()
    return True


async def list_keys(db: AsyncSession, member: Member) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT k.provider, k.hint, k.created_at, k.verified_at, u.display_name AS created_by_name"
            " FROM provider_keys k LEFT JOIN users u ON u.id = k.created_by"
            " WHERE k.workspace_id = :w AND k.revoked_at IS NULL ORDER BY k.provider"
        ),
        {"w": member.workspace_id},
    )
    return [dict(r._mapping) for r in rows]


async def save_key(
    db: AsyncSession, sealer: Sealer, verifier: Verifier, member: Member, provider: str, key: str
) -> dict[str, Any]:
    key = key.strip()
    if provider not in PROVIDERS:
        raise ApiError(404, "NOT_FOUND", "Unknown provider.")
    if not (20 <= len(key) <= 512) or not key.isascii() or any(c.isspace() for c in key):
        raise ApiError(422, "KEY_INVALID", "That does not look like an API key.")
    await db.commit()  # no transaction is open during the provider call
    try:
        accepted = await verifier(provider, key)
    except Exception as error:  # an outage: the key is not saved, the caller may retry
        logger.warning("provider key check failed", extra={"provider": provider})
        raise ApiError(
            502, "PROVIDER_UNAVAILABLE", "The provider could not check the key. Try again.", retryable=True
        ) from error
    if not accepted:
        raise ApiError(422, "KEY_REJECTED", "The provider refused this key.")
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    await db.execute(
        text(
            "UPDATE provider_keys SET revoked_at = now(), sealed_key = NULL"
            " WHERE workspace_id = :w AND provider = :p AND revoked_at IS NULL"
        ),
        {"w": member.workspace_id, "p": provider},
    )
    await db.execute(
        text(
            "INSERT INTO provider_keys(workspace_id, provider, sealed_key, hint, created_by, verified_at)"
            " VALUES (:w, :p, :k, :h, :u, now())"
        ),
        {
            "w": member.workspace_id,
            "p": provider,
            "k": sealer.seal(key.encode(), associated(member.workspace_id, provider)),
            "h": hint(key),
            "u": member.user_id,
        },
    )
    await audit(
        db,
        member,
        "provider_key.saved",
        "workspace",
        member.workspace_id,
        {"provider": provider, "hint": hint(key)},
    )
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return next(k for k in await list_keys(db, member) if k["provider"] == provider)


async def remove_key(db: AsyncSession, member: Member, provider: str) -> None:
    removed = (
        await db.execute(
            text(
                "UPDATE provider_keys SET revoked_at = now(), sealed_key = NULL"
                " WHERE workspace_id = :w AND provider = :p AND revoked_at IS NULL RETURNING hint"
            ),
            {"w": member.workspace_id, "p": provider},
        )
    ).scalar_one_or_none()
    if removed is None:
        raise ApiError(404, "NOT_FOUND", "No key is stored for this provider.")
    await audit(
        db,
        member,
        "provider_key.removed",
        "workspace",
        member.workspace_id,
        {"provider": provider, "hint": removed},
    )
    await db.commit()


async def active_providers(db: AsyncSession, workspace_id: UUID) -> set[str]:
    rows = await db.execute(
        text("SELECT provider FROM provider_keys WHERE workspace_id = :w AND revoked_at IS NULL"),
        {"w": workspace_id},
    )
    return {r.provider for r in rows}


async def open_key(db: AsyncSession, sealer: Sealer, workspace_id: UUID, provider: str) -> str | None:
    """The plaintext key for a build of this workspace, or None when there is none (caller set context)."""
    sealed = (
        await db.execute(
            text(
                "SELECT sealed_key FROM provider_keys WHERE workspace_id = :w AND provider = :p AND revoked_at IS NULL"
            ),
            {"w": workspace_id, "p": provider},
        )
    ).scalar_one_or_none()
    if sealed is None:
        return None
    return sealer.open(bytes(sealed), associated(workspace_id, provider)).decode()
