"""Organizations and handles (0038; 01, 03, 06, 09).

A handle names a person or an organization in one global namespace; it appears in site addresses. An
organization is a level above workspaces: its owners and admins are owners and admins of every workspace
it holds (the database keeps those memberships in step), and its plain members see only the workspaces
they are added to. Organization changes run in the organization's context (`app.organization_id`), set
only after the caller's role was checked here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.modules.identity.sessions import Principal

OrgRole = Literal["owner", "admin", "member"]
RANK = {"member": 1, "admin": 2, "owner": 3}
HANDLE_ERRORS = {
    "invalid": (
        422,
        "HANDLE_INVALID",
        "Use 1-39 lowercase letters, digits or single hyphens, not starting or ending with one, and not a"
        " reserved word.",
    ),
    "taken": (409, "HANDLE_TAKEN", "That name is taken."),
    "reserved": (409, "HANDLE_TAKEN", "That name was released recently and is reserved."),
}


@dataclass(frozen=True)
class OrgMember:
    principal: Principal
    organization_id: UUID
    role: OrgRole

    @property
    def user_id(self) -> UUID:
        return self.principal.user_id

    def at_least(self, role: OrgRole) -> bool:
        return RANK[self.role] >= RANK[role]


def _handle_error(reason: str) -> ApiError:
    status, code, message = HANDLE_ERRORS[reason]
    return ApiError(status, code, message, details={"field": "handle"})


async def handle_unavailable(db: AsyncSession, handle: str, namespace_id: UUID | None = None) -> str | None:
    reason: str | None = (
        await db.execute(
            text("SELECT app_private.handle_unavailable(:h, :n)"), {"h": handle, "n": namespace_id}
        )
    ).scalar_one()
    return reason


async def _account_audit(
    db: AsyncSession, actor: UUID, action: str, organization_id: UUID, **meta: Any
) -> None:
    """Organization events are account-level audit events (no workspace), insert-only like the others."""
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata)"
            " VALUES (NULL, :u, :a, 'organization', :o, CAST(:m AS jsonb))"
        ),
        {"u": actor, "a": action, "o": organization_id, "m": json.dumps(meta)},
    )


# ---------------------------------------------------------------- handles
async def own_namespace(db: AsyncSession, user_id: UUID) -> UUID:
    namespace: UUID = (
        await db.execute(text("SELECT id FROM namespaces WHERE user_id = :u"), {"u": user_id})
    ).scalar_one()
    return namespace


async def rename(db: AsyncSession, principal: Principal, namespace_id: UUID, handle: str) -> str:
    reason = await handle_unavailable(db, handle, namespace_id)
    if reason:
        raise _handle_error(reason)
    await set_context(db, user_id=principal.user_id)
    try:
        renamed: str = (
            await db.execute(
                text("SELECT app_private.rename_namespace(:n, :h)"), {"n": namespace_id, "h": handle}
            )
        ).scalar_one()
    except exc.IntegrityError as error:
        await db.rollback()
        raise _handle_error("taken") from error
    except exc.DBAPIError as error:
        await db.rollback()
        if getattr(error.orig, "sqlstate", "") == "42501":
            raise ApiError(403, "FORBIDDEN", "Only an owner can rename this.") from error
        raise
    return renamed


# ---------------------------------------------------------------- access
async def member(
    db: AsyncSession, principal: Principal, organization_id: UUID, minimum: OrgRole = "member"
) -> OrgMember:
    """The caller's role in a live organization; 404 for a non-member, 403 below `minimum`."""
    await set_context(db, user_id=principal.user_id)
    row = (
        await db.execute(
            text(
                "SELECT m.role FROM organization_members m JOIN organizations o ON o.id = m.organization_id"
                " WHERE m.organization_id = :o AND m.user_id = :u AND o.status = 'active'"
            ),
            {"o": organization_id, "u": principal.user_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Organization not found.")
    found = OrgMember(principal, organization_id, row.role)
    if not found.at_least(minimum):
        raise ApiError(403, "FORBIDDEN", "Your organization role does not allow this.")
    await set_context(db, user_id=principal.user_id, organization_id=organization_id)
    return found


# ---------------------------------------------------------------- organizations
async def create(db: AsyncSession, principal: Principal, name: str, handle: str) -> UUID:
    reason = await handle_unavailable(db, handle)
    if reason:
        raise _handle_error(reason)
    await set_context(db, user_id=principal.user_id)
    try:
        organization_id: UUID = (
            await db.execute(
                text("SELECT app_private.create_organization(:n, :h)"), {"n": name.strip(), "h": handle}
            )
        ).scalar_one()
    except exc.IntegrityError as error:
        await db.rollback()
        if "organizations_name_check" in str(error.orig):
            raise ApiError(
                422, "VALIDATION_FAILED", "Give the organization a name of 2-80 characters."
            ) from error
        raise _handle_error("taken") from error
    await _account_audit(db, principal.user_id, "organization.created", organization_id, handle=handle)
    await db.commit()
    return organization_id


async def listing(db: AsyncSession, principal: Principal) -> dict[str, Any]:
    await set_context(db, user_id=principal.user_id)
    organizations = await db.execute(
        text(
            "SELECT o.id, o.name, n.handle, m.role FROM organization_members m"
            " JOIN organizations o ON o.id = m.organization_id AND o.status = 'active'"
            " JOIN namespaces n ON n.organization_id = o.id"
            " WHERE m.user_id = :u ORDER BY n.handle"
        ),
        {"u": principal.user_id},
    )
    invitations = await db.execute(
        text(
            "SELECT i.id, i.role, i.expires_at, o.id AS organization_id, o.name, n.handle"
            " FROM organization_invitations i JOIN organizations o ON o.id = i.organization_id AND o.status = 'active'"
            " JOIN namespaces n ON n.organization_id = o.id"
            " WHERE i.user_id = :u AND i.accepted_at IS NULL AND i.declined_at IS NULL AND i.revoked_at IS NULL"
            " AND i.expires_at > now() ORDER BY i.created_at"
        ),
        {"u": principal.user_id},
    )
    result = {
        "items": [dict(r._mapping) for r in organizations],
        "invitations": [
            {
                "id": r.id,
                "role": r.role,
                "expires_at": r.expires_at,
                "organization": {"id": r.organization_id, "name": r.name, "handle": r.handle},
            }
            for r in invitations
        ],
    }
    await db.commit()
    return result


async def get(db: AsyncSession, found: OrgMember) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT o.id, o.name, n.handle, n.id AS namespace_id, o.created_at FROM organizations o"
                " JOIN namespaces n ON n.organization_id = o.id WHERE o.id = :o"
            ),
            {"o": found.organization_id},
        )
    ).one()
    return {**dict(row._mapping), "role": found.role}


async def rename_organization(db: AsyncSession, found: OrgMember, name: str) -> None:
    try:
        await db.execute(
            text("UPDATE organizations SET name = :n, updated_at = now() WHERE id = :o"),
            {"n": name.strip(), "o": found.organization_id},
        )
    except exc.IntegrityError as error:
        await db.rollback()
        raise ApiError(
            422, "VALIDATION_FAILED", "Give the organization a name of 2-80 characters."
        ) from error
    await _account_audit(db, found.user_id, "organization.renamed", found.organization_id)
    await db.commit()


async def delete(db: AsyncSession, found: OrgMember) -> None:
    """Owners only, and only once the organization holds no live workspace. Its handle is reserved for
    nobody for 90 days; members and open invitations go."""
    live = (
        await db.execute(
            text(
                "SELECT count(*) FROM workspaces w JOIN namespaces n ON n.id = w.namespace_id"
                " WHERE n.organization_id = :o AND w.deleted_at IS NULL"
            ),
            {"o": found.organization_id},
        )
    ).scalar_one()
    if live:
        raise ApiError(409, "ORGANIZATION_NOT_EMPTY", "Delete or move this organization's workspaces first.")
    namespace = (
        await db.execute(
            text("SELECT id FROM namespaces WHERE organization_id = :o"), {"o": found.organization_id}
        )
    ).scalar_one()
    await db.execute(
        text(
            "UPDATE organizations SET status = 'deleting', deleted_at = now(), updated_at = now() WHERE id = :o"
        ),
        {"o": found.organization_id},
    )
    await db.execute(text("SELECT app_private.retire_namespace(:n)"), {"n": namespace})
    await db.execute(
        text(
            "UPDATE organization_invitations SET revoked_at = now() WHERE organization_id = :o"
            " AND accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL"
        ),
        {"o": found.organization_id},
    )
    await db.execute(
        text("DELETE FROM organization_members WHERE organization_id = :o"), {"o": found.organization_id}
    )
    await _account_audit(db, found.user_id, "organization.deleted", found.organization_id)
    await db.commit()


async def workspaces(db: AsyncSession, found: OrgMember) -> list[dict[str, Any]]:
    """The organization's workspaces the caller belongs to (owners and admins belong to all of them)."""
    rows = await db.execute(
        text(
            "SELECT w.id, w.name, w.slug, m.role FROM workspaces w"
            " JOIN namespaces n ON n.id = w.namespace_id AND n.organization_id = :o"
            " JOIN memberships m ON m.workspace_id = w.id AND m.user_id = :u"
            " WHERE w.status = 'active' ORDER BY w.created_at, w.id"
        ),
        {"o": found.organization_id, "u": found.user_id},
    )
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------- members
async def members(db: AsyncSession, found: OrgMember) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT m.user_id, u.display_name, n.handle, m.role, m.created_at FROM organization_members m"
            " JOIN users u ON u.id = m.user_id JOIN namespaces n ON n.user_id = m.user_id"
            " WHERE m.organization_id = :o ORDER BY m.created_at, m.id"
        ),
        {"o": found.organization_id},
    )
    return [dict(r._mapping) for r in rows]


async def _target(db: AsyncSession, found: OrgMember, user_id: UUID) -> str:
    await db.execute(
        text("SELECT 1 FROM organizations WHERE id = :o FOR UPDATE"), {"o": found.organization_id}
    )
    role = (
        await db.execute(
            text(
                "SELECT role FROM organization_members WHERE organization_id = :o AND user_id = :u FOR UPDATE"
            ),
            {"o": found.organization_id, "u": user_id},
        )
    ).scalar_one_or_none()
    if role is None:
        raise ApiError(404, "NOT_FOUND", "Member not found.")
    return str(role)


async def _owners(db: AsyncSession, organization_id: UUID) -> int:
    count: int = (
        await db.execute(
            text("SELECT count(*) FROM organization_members WHERE organization_id = :o AND role = 'owner'"),
            {"o": organization_id},
        )
    ).scalar_one()
    return count


async def change_role(db: AsyncSession, found: OrgMember, user_id: UUID, role: OrgRole) -> None:
    """Owners change roles; the last owner stays an owner."""
    if not found.at_least("owner"):
        raise ApiError(403, "FORBIDDEN", "Only an owner can change roles.")
    current = await _target(db, found, user_id)
    if current == role:
        return
    if current == "owner" and await _owners(db, found.organization_id) == 1:
        raise ApiError(409, "LAST_OWNER", "An organization must keep at least one owner.")
    await db.execute(
        text(
            "UPDATE organization_members SET role = :r, updated_at = now() WHERE organization_id = :o AND user_id = :u"
        ),
        {"r": role, "o": found.organization_id, "u": user_id},
    )
    await _account_audit(
        db, found.user_id, "organization.role_changed", found.organization_id, user_id=str(user_id), to=role
    )
    await db.commit()


async def remove_member(db: AsyncSession, found: OrgMember, user_id: UUID) -> None:
    """Anyone may leave; owners remove anyone, admins remove plain members. Leaving ends the access the
    organization gave in its workspaces (the database removes or lowers those memberships)."""
    current = await _target(db, found, user_id)
    leaving = user_id == found.user_id
    if not leaving and not (found.at_least("owner") or (found.role == "admin" and current == "member")):
        raise ApiError(403, "FORBIDDEN", "Your organization role does not allow removing this member.")
    if current == "owner" and await _owners(db, found.organization_id) == 1:
        raise ApiError(409, "LAST_OWNER", "An organization must keep at least one owner.")
    await db.execute(
        text("DELETE FROM organization_members WHERE organization_id = :o AND user_id = :u"),
        {"o": found.organization_id, "u": user_id},
    )
    await _account_audit(
        db,
        found.user_id,
        "organization.member_left" if leaving else "organization.member_removed",
        found.organization_id,
        user_id=str(user_id),
    )
    await db.commit()


# ---------------------------------------------------------------- invitations
async def invite(db: AsyncSession, found: OrgMember, handle: str, role: OrgRole) -> dict[str, Any]:
    """Owners invite with any role, admins invite plain members. The person accepts in the app."""
    if not (found.at_least("owner") or (found.role == "admin" and role == "member")):
        raise ApiError(403, "FORBIDDEN", "Your organization role does not allow inviting with this role.")
    person = (
        await db.execute(
            text("SELECT user_id FROM namespaces WHERE handle = :h AND kind = 'user'"),
            {"h": handle.strip().lower()},
        )
    ).scalar_one_or_none()
    if person is None:
        raise ApiError(404, "USER_NOT_FOUND", "No person has that name.", details={"field": "handle"})
    already = (
        await db.execute(
            text("SELECT 1 FROM organization_members WHERE organization_id = :o AND user_id = :u"),
            {"o": found.organization_id, "u": person},
        )
    ).scalar_one_or_none()
    if already:
        raise ApiError(409, "ALREADY_MEMBER", "That person is already a member.")
    try:
        row = (
            await db.execute(
                text(
                    "INSERT INTO organization_invitations(organization_id, user_id, role, invited_by)"
                    " VALUES (:o, :u, :r, :by) RETURNING id, role, created_at, expires_at"
                ),
                {"o": found.organization_id, "u": person, "r": role, "by": found.user_id},
            )
        ).one()
    except exc.IntegrityError as error:
        await db.rollback()
        raise ApiError(409, "ALREADY_INVITED", "That person already has an open invitation.") from error
    await _account_audit(
        db, found.user_id, "organization.invited", found.organization_id, user_id=str(person), role=role
    )
    await db.commit()
    return {**dict(row._mapping), "handle": handle.strip().lower()}


async def invitations(db: AsyncSession, found: OrgMember) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT i.id, n.handle, i.role, i.created_at, i.expires_at FROM organization_invitations i"
            " JOIN namespaces n ON n.user_id = i.user_id"
            " WHERE i.organization_id = :o AND i.accepted_at IS NULL AND i.declined_at IS NULL"
            " AND i.revoked_at IS NULL AND i.expires_at > now() ORDER BY i.created_at"
        ),
        {"o": found.organization_id},
    )
    return [dict(r._mapping) for r in rows]


async def revoke_invitation(db: AsyncSession, found: OrgMember, invitation_id: UUID) -> None:
    if not found.at_least("admin"):
        raise ApiError(403, "FORBIDDEN", "Only owners and admins manage invitations.")
    row = (
        await db.execute(
            text(
                "UPDATE organization_invitations SET revoked_at = now() WHERE organization_id = :o AND id = :i"
                " AND accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL RETURNING id"
            ),
            {"o": found.organization_id, "i": invitation_id},
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Invitation not found.")
    await db.commit()


async def answer(db: AsyncSession, principal: Principal, invitation_id: UUID, accept: bool) -> UUID:
    await set_context(db, user_id=principal.user_id)
    organization_id = (
        await db.execute(
            text("SELECT app_private.answer_organization_invitation(:i, :a)"),
            {"i": invitation_id, "a": accept},
        )
    ).scalar_one()
    if organization_id is None:
        await db.rollback()
        raise ApiError(404, "NOT_FOUND", "Invitation not found.")
    await _account_audit(
        db,
        principal.user_id,
        "organization.invitation_accepted" if accept else "organization.invitation_declined",
        organization_id,
    )
    await db.commit()
    return UUID(str(organization_id))
