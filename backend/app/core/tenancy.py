"""Tenant context for row-level security (05 "Tenant context resolution and database roles").

Every transaction that touches tenant data sets its context first. The settings are transaction-local
(`set_config(..., true)`), so they vanish on commit/rollback and cannot leak through the connection
pool. Values are bound parameters; never format them into SQL. A missing setting denies all rows.

The workspace comes from the authorized session or a claimed task, never from request data alone:
for flat routes, resolve the owning workspace, check membership, then set the context.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ResourceKind = Literal["project", "build"]

_SET_CONTEXT = text(
    "SELECT set_config('app.workspace_id', :workspace_id, true), set_config('app.user_id', :user_id, true)"
)
_RESOLVE = text("SELECT app_private.resource_workspace(:kind, :resource_id)")


async def set_context(
    session: AsyncSession, *, workspace_id: UUID | None = None, user_id: UUID | None = None
) -> None:
    """Set (or clear, with None) the RLS context for the session's current transaction."""
    await session.execute(
        _SET_CONTEXT,
        {
            "workspace_id": str(workspace_id) if workspace_id else "",
            "user_id": str(user_id) if user_id else "",
        },
    )


async def resource_workspace(session: AsyncSession, kind: ResourceKind, resource_id: UUID) -> UUID | None:
    """Owning workspace of a resource, or None if it does not exist. Not an authorization decision."""
    result = await session.execute(_RESOLVE, {"kind": kind, "resource_id": resource_id})
    value = result.scalar_one()
    return value if value is None or isinstance(value, UUID) else UUID(str(value))
