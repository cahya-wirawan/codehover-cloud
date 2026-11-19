"""Per-project access control (0027; 01 "per-project ACLs are P1", 09).

A project is open to its whole workspace (`workspace`, the default) or `restricted`. Restricted projects
are visible to workspace admins and owners, and to the members granted access as viewer or editor, which
is then their role on that project (a workspace viewer granted `editor` can build there; a workspace
editor granted `viewer` can only read). Admins manage access; the mode change is guarded by an ETag.
Who can see a project is decided by `app_private.project_role` everywhere.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

Mode = Literal["workspace", "restricted"]
GrantRole = Literal["viewer", "editor"]


def access_etag(version: int) -> str:
    return f'"project-access-{version}"'


async def get_access(db: AsyncSession, member: Member, project_id: UUID) -> dict[str, Any]:
    project = (
        await db.execute(
            text(
                "SELECT access, access_version FROM projects WHERE workspace_id = :w AND id = :p AND deleted_at IS NULL"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if project is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    grants = await db.execute(
        text(
            "SELECT g.user_id, u.display_name AS name, g.role, g.created_at, m.role AS workspace_role"
            " FROM project_members g JOIN users u ON u.id = g.user_id"
            " JOIN memberships m ON m.workspace_id = g.workspace_id AND m.user_id = g.user_id"
            " WHERE g.workspace_id = :w AND g.project_id = :p ORDER BY u.display_name, g.user_id"
        ),
        {"w": member.workspace_id, "p": project_id},
    )
    await db.commit()
    return {
        "mode": project.access,
        "version": project.access_version,
        "members": [dict(g._mapping) for g in grants],
    }


async def set_mode(
    db: AsyncSession, member: Member, project_id: UUID, mode: Mode, if_match: str | None
) -> dict[str, Any]:
    current = (
        await db.execute(
            text(
                "SELECT access, access_version FROM projects WHERE workspace_id = :w AND id = :p AND deleted_at IS NULL"
                " FOR UPDATE"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if current is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    if if_match is None:
        raise ApiError(428, "PRECONDITION_REQUIRED", "Send If-Match with the current access ETag.")
    if if_match != access_etag(current.access_version):
        raise ApiError(
            409, "VERSION_CONFLICT", "Access changed meanwhile.", details={"version": current.access_version}
        )
    if current.access != mode:
        await db.execute(
            text(
                "UPDATE projects SET access = :m, access_version = access_version + 1, updated_at = now()"
                " WHERE workspace_id = :w AND id = :p"
            ),
            {"m": mode, "w": member.workspace_id, "p": project_id},
        )
        await audit(db, member, "project.access_changed", "project", project_id, {"mode": mode})
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_access(db, member, project_id)


async def grant(
    db: AsyncSession, member: Member, project_id: UUID, user_id: UUID, role: GrantRole
) -> dict[str, Any]:
    target = (
        await db.execute(
            text("SELECT role FROM memberships WHERE workspace_id = :w AND user_id = :u"),
            {"w": member.workspace_id, "u": user_id},
        )
    ).scalar_one_or_none()
    if target is None:
        raise ApiError(404, "NOT_FOUND", "Not a member of this workspace.")
    if target in ("admin", "owner"):
        raise ApiError(409, "ALREADY_FULL_ACCESS", "Admins and owners can open every project already.")
    await db.execute(
        text(
            "INSERT INTO project_members(workspace_id, project_id, user_id, role, granted_by)"
            " VALUES (:w, :p, :u, :r, :by) ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role"
        ),
        {"w": member.workspace_id, "p": project_id, "u": user_id, "r": role, "by": member.user_id},
    )
    await audit(
        db, member, "project.member_granted", "project", project_id, {"user_id": str(user_id), "role": role}
    )
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_access(db, member, project_id)


async def revoke(db: AsyncSession, member: Member, project_id: UUID, user_id: UUID) -> None:
    removed = (
        await db.execute(
            text(
                "DELETE FROM project_members WHERE workspace_id = :w AND project_id = :p AND user_id = :u RETURNING id"
            ),
            {"w": member.workspace_id, "p": project_id, "u": user_id},
        )
    ).scalar_one_or_none()
    if removed is None:
        raise ApiError(404, "NOT_FOUND", "This member has no grant on the project.")
    await audit(db, member, "project.member_revoked", "project", project_id, {"user_id": str(user_id)})
    await db.commit()
