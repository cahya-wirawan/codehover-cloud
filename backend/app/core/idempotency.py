"""Idempotent creates (06 "Shared behavior"): `Idempotency-Key` (16-128 chars) scoped to the user and
route, request digest stored 24 h. Same key and body returns the original response; a different body
is 409; a concurrent duplicate while the first is in flight is 409 IDEMPOTENCY_IN_PROGRESS."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.tenancy import set_context

TTL_SECONDS = 24 * 3600


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def run_once(
    db: AsyncSession,
    *,
    user_id: UUID,
    workspace_id: UUID,
    route: str,
    key: str | None,
    body: dict[str, Any],
    produce: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
    stored: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """`stored` transforms the response kept for replays (a secret shown once is removed from it)."""
    if key is None or not 16 <= len(key) <= 128:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header (16-128 characters).")
    digest = _digest(body)
    params = {"u": user_id, "w": workspace_id, "r": route, "k": key}
    await set_context(db, workspace_id=workspace_id, user_id=user_id)
    existing = (
        await db.execute(
            text(
                "SELECT request_digest, response_status, response_body FROM idempotency_keys"
                " WHERE user_id = :u AND route = :r AND key = :k AND expires_at > now()"
            ),
            params,
        )
    ).one_or_none()
    if existing is not None:
        await db.commit()
        if existing.request_digest != digest:
            raise ApiError(
                409, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used with a different request."
            )
        if existing.response_status is None:
            raise ApiError(409, "IDEMPOTENCY_IN_PROGRESS", "The original request is still being processed.")
        replay: dict[str, Any] = existing.response_body
        return int(existing.response_status), replay
    try:
        await db.execute(
            text("DELETE FROM idempotency_keys WHERE user_id = :u AND route = :r AND key = :k"), params
        )
        await db.execute(
            text(
                "INSERT INTO idempotency_keys(user_id, workspace_id, route, key, request_digest, expires_at)"
                " VALUES (:u, :w, :r, :k, :d, now() + make_interval(secs => :ttl))"
            ),
            {**params, "d": digest, "ttl": TTL_SECONDS},
        )
        await db.commit()
    except exc.IntegrityError as error:
        await db.rollback()
        raise ApiError(
            409, "IDEMPOTENCY_IN_PROGRESS", "The original request is still being processed."
        ) from error
    await set_context(db, workspace_id=workspace_id, user_id=user_id)  # the commit above cleared it
    try:
        status, response = await produce()
    except Exception:
        await set_context(db, workspace_id=workspace_id, user_id=user_id)
        await db.execute(
            text("DELETE FROM idempotency_keys WHERE user_id = :u AND route = :r AND key = :k"), params
        )
        await db.commit()
        raise
    await set_context(db, workspace_id=workspace_id, user_id=user_id)
    await db.execute(
        text(
            "UPDATE idempotency_keys SET response_status = :s, response_body = CAST(:b AS jsonb)"
            " WHERE user_id = :u AND route = :r AND key = :k"
        ),
        {**params, "s": status, "b": json.dumps(stored(response) if stored else response, default=str)},
    )
    await db.commit()
    return status, response
