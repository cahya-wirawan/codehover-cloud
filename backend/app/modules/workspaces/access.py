"""Workspace access: resolve the caller's role, then open the tenant context (05, 09).

The membership lookup runs in user scope (own memberships only), so a workspace the caller does not
belong to is indistinguishable from one that does not exist: both are 404. Role failures inside a
workspace the caller can see are 403.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import Depends
from sqlalchemy import text

from app.core.errors import ApiError
from app.core.request_context import current_request_id
from app.core.tenancy import set_context
from app.modules.identity.sessions import CsrfPrincipal, CurrentPrincipal, Db, Principal

Role = Literal["viewer", "editor", "admin", "owner"]
ROLE_RANK: dict[str, int] = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}


@dataclass(frozen=True)
class Member:
    principal: Principal
    workspace_id: UUID
    role: Role
    support: bool = False  # a platform operator under the workspace's support grant: read-only viewer

    @property
    def user_id(self) -> UUID:
        return self.principal.user_id

    def at_least(self, role: Role) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


class Reader(Protocol):
    """Read access to one workspace's build content: a member, or the publication resolver for a public
    site (05 "Published sites"). Read paths use only the workspace; they never act for a user."""

    @property
    def workspace_id(self) -> UUID: ...


@dataclass(frozen=True)
class PublicReader:
    """A visitor of an explicitly public publication; the workspace comes from the resolver, never from
    request data."""

    workspace_id: UUID


def require_role(member: Member, minimum: Role) -> None:
    if not member.at_least(minimum):
        raise ApiError(403, "FORBIDDEN", "Your role does not allow this action.")


async def _member(workspace_id: UUID, db: Db, principal: Principal, *, write: bool = False) -> Member:
    role = (
        await db.execute(
            text(
                "SELECT m.role FROM memberships m JOIN workspaces w ON w.id = m.workspace_id"
                " WHERE m.workspace_id = :w AND m.user_id = :u AND w.status = 'active'"
            ),
            {"w": workspace_id, "u": principal.user_id},
        )
    ).scalar_one_or_none()
    if role is None:
        if not write and await _support_access(db, workspace_id, principal):
            return Member(principal, workspace_id, "viewer", support=True)
        raise ApiError(404, "NOT_FOUND", "Workspace not found.")
    await set_context(db, workspace_id=workspace_id, user_id=principal.user_id)
    return Member(principal, workspace_id, role)


SUPPORT_AUDIT_INTERVAL = "10 minutes"


async def _support_access(db: Db, workspace_id: UUID, principal: Principal) -> bool:
    """An operator reads a workspace only under its live support grant (08), never writes, and every
    session of reading is in the workspace's audit log (at most one event per 10 minutes)."""
    allowed = (
        await db.execute(text("SELECT app_private.support_access(:w)"), {"w": workspace_id})
    ).scalar_one()
    if not allowed:
        return False
    await set_context(db, workspace_id=workspace_id, user_id=principal.user_id)
    recent = (
        await db.execute(
            text(
                "SELECT 1 FROM audit_events WHERE workspace_id = :w AND actor_user_id = :u"
                " AND action = 'support.accessed' AND created_at > now() - CAST(:i AS interval) LIMIT 1"
            ),
            {"w": workspace_id, "u": principal.user_id, "i": SUPPORT_AUDIT_INTERVAL},
        )
    ).one_or_none()
    if recent is None:
        await db.execute(
            text(
                "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, metadata, request_id)"
                " VALUES (:w, :u, 'support.accessed', 'workspace', '{\"operator\": true}', :r)"
            ),
            {"w": workspace_id, "u": principal.user_id, "r": current_request_id()},
        )
        await db.commit()  # read handlers may not commit; the context is transaction-local, so set it again
        await set_context(db, workspace_id=workspace_id, user_id=principal.user_id)
    return True


async def read_member(workspace_id: UUID, db: Db, principal: CurrentPrincipal) -> Member:
    return await _member(workspace_id, db, principal)


async def write_member(workspace_id: UUID, db: Db, principal: CsrfPrincipal) -> Member:
    return await _member(workspace_id, db, principal, write=True)


ReadMember = Annotated[Member, Depends(read_member)]
WriteMember = Annotated[Member, Depends(write_member)]


async def resource_member(
    kind: str, resource_id: UUID, db: Db, principal: Principal, *, write: bool = False
) -> Member:
    """Flat routes (/projects/{id}, /builds/{id}): resolve the owning workspace, then require membership.
    Unknown resources and resources in workspaces the caller does not belong to are both 404 (05)."""
    from app.core.tenancy import resource_workspace

    workspace_id = await resource_workspace(db, kind, resource_id)  # type: ignore[arg-type]
    if workspace_id is None:
        raise ApiError(404, "NOT_FOUND", "Not found.")
    return await _member(workspace_id, db, principal, write=write)
