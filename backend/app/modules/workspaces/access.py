"""Workspace access: resolve the caller's role, then open the tenant context (05, 09).

The membership lookup runs in user scope (own memberships only), so a workspace the caller does not
belong to is indistinguishable from one that does not exist: both are 404. Role failures inside a
workspace the caller can see are 403.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends
from sqlalchemy import text

from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.modules.identity.sessions import CsrfPrincipal, CurrentPrincipal, Db, Principal

Role = Literal["viewer", "editor", "admin", "owner"]
ROLE_RANK: dict[str, int] = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}


@dataclass(frozen=True)
class Member:
    principal: Principal
    workspace_id: UUID
    role: Role

    @property
    def user_id(self) -> UUID:
        return self.principal.user_id

    def at_least(self, role: Role) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def require_role(member: Member, minimum: Role) -> None:
    if not member.at_least(minimum):
        raise ApiError(403, "FORBIDDEN", "Your role does not allow this action.")


async def _member(workspace_id: UUID, db: Db, principal: Principal) -> Member:
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
        raise ApiError(404, "NOT_FOUND", "Workspace not found.")
    await set_context(db, workspace_id=workspace_id, user_id=principal.user_id)
    return Member(principal, workspace_id, role)


async def read_member(workspace_id: UUID, db: Db, principal: CurrentPrincipal) -> Member:
    return await _member(workspace_id, db, principal)


async def write_member(workspace_id: UUID, db: Db, principal: CsrfPrincipal) -> Member:
    return await _member(workspace_id, db, principal)


ReadMember = Annotated[Member, Depends(read_member)]
WriteMember = Annotated[Member, Depends(write_member)]
