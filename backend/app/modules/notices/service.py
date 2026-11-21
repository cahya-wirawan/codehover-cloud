"""Record owner notices. Idempotent per `dedupe_key`: the same change seen twice records one notice."""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

Kind = Literal["site_suspended", "billing_past_due", "dispute_opened", "dispute_lost"]


async def record(
    db: AsyncSession, workspace_id: UUID, kind: Kind, dedupe_key: str, details: dict[str, Any] | None = None
) -> bool:
    """Record a notice in the caller's transaction (workspace context set). True when it is new."""
    created = (
        await db.execute(
            text(
                "INSERT INTO owner_notices(workspace_id, kind, dedupe_key, details)"
                " VALUES (:w, :k, :d, CAST(:m AS jsonb))"
                " ON CONFLICT (workspace_id, dedupe_key) DO NOTHING RETURNING id"
            ),
            {
                "w": workspace_id,
                "k": kind,
                "d": f"{kind}:{dedupe_key}"[:200],
                "m": json.dumps(details or {}, default=str, sort_keys=True),
            },
        )
    ).scalar_one_or_none()
    return created is not None
