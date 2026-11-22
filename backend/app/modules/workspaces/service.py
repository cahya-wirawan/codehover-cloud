"""Workspace, membership and invitation rules (09 "Permission matrix", 03 "Purchases and membership").

Role rules:
- Admins manage viewers and editors only, and can only grant viewer/editor.
- Owners manage everyone, including admins and other owners.
- Anyone may leave; the last owner can neither leave nor be demoted (also enforced by a deferred
  database trigger that serializes concurrent removals).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.pagination import Position
from app.core.request_context import current_request_id
from app.core.security import digest, new_token
from app.core.tenancy import set_context
from app.modules.identity.sessions import Principal
from app.modules.mcp import tokens
from app.modules.workspaces.access import ROLE_RANK, Member, Role

INVITATION_LIFETIME = timedelta(days=7)
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]+$")
MANAGEABLE_BY_ADMIN: frozenset[str] = frozenset({"viewer", "editor"})


# ---------------------------------------------------------------- permission matrix
def can_manage(actor: Member, target_role: str, new_role: str | None = None) -> bool:
    """May `actor` change (or, with new_role None, remove) a member whose role is `target_role`?"""
    if actor.role == "owner":
        return True
    if actor.role == "admin":
        return target_role in MANAGEABLE_BY_ADMIN and (new_role is None or new_role in MANAGEABLE_BY_ADMIN)
    return False


def can_invite(actor: Member, role: str) -> bool:
    return actor.role == "owner" or (actor.role == "admin" and role in MANAGEABLE_BY_ADMIN)


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise ApiError(422, "VALIDATION_FAILED", "Enter a valid email address.", details={"field": "email"})
    return email


def etag(updated_at: datetime) -> str:
    return f'"{int(updated_at.timestamp() * 1_000_000)}"'


async def audit(
    db: AsyncSession,
    member: Member,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, resource_id,"
            " metadata, request_id) VALUES (:w, :u, :a, :t, :r, CAST(:m AS jsonb), :req)"
        ),
        {
            "w": member.workspace_id,
            "u": member.user_id,
            "a": action,
            "t": resource_type,
            "r": resource_id,
            "m": json.dumps(metadata or {}),
            "req": current_request_id(),
        },
    )


def _is_owner_invariant(error: exc.DBAPIError) -> bool:
    diag = getattr(error.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == "memberships_keep_owner"


async def commit_or_conflict(db: AsyncSession) -> None:
    """Commit; the deferred owner trigger fires here and becomes a 409, not a 500."""
    try:
        await db.commit()
    except exc.IntegrityError as error:
        await db.rollback()
        if _is_owner_invariant(error):
            raise ApiError(409, "LAST_OWNER", "A workspace must keep at least one owner.") from error
        raise


# ---------------------------------------------------------------- workspaces
async def create_workspace(
    db: AsyncSession, principal: Principal, name: str, slug: str, organization_id: UUID | None = None
) -> UUID:
    """In the caller's personal namespace, or in an organization they own or administer (0038)."""
    await set_context(db, user_id=principal.user_id)
    try:
        workspace_id: UUID = (
            await db.execute(
                text("SELECT app_private.create_workspace(:n, :s, :o)"),
                {"n": name, "s": slug, "o": organization_id},
            )
        ).scalar_one()
    except exc.IntegrityError as error:
        await db.rollback()
        if "uq_workspaces_live_slug" in str(error.orig):  # unique among live workspaces (0015)
            raise ApiError(409, "SLUG_TAKEN", "That workspace address is already in use.") from error
        raise ApiError(422, "VALIDATION_FAILED", "Workspace name or address is invalid.") from error
    except exc.DBAPIError as error:
        await db.rollback()
        if getattr(error.orig, "sqlstate", "") == "42501":
            raise ApiError(
                403, "FORBIDDEN", "Only an organization's owners and admins create its workspaces."
            ) from error
        raise
    await set_context(db, workspace_id=workspace_id, user_id=principal.user_id)
    role = (
        await db.execute(
            text("SELECT role FROM memberships WHERE workspace_id = :w AND user_id = :u"),
            {"w": workspace_id, "u": principal.user_id},
        )
    ).scalar_one()
    member = Member(principal, workspace_id, role)
    await audit(
        db,
        member,
        "workspace.created",
        "workspace",
        workspace_id,
        {"slug": slug, **({"organization_id": str(organization_id)} if organization_id else {})},
    )
    await db.commit()
    return workspace_id


def _with_owner(row: dict[str, Any]) -> dict[str, Any]:
    kind, handle, org = (
        row.pop("owner_kind", None),
        row.pop("owner_handle", None),
        row.pop("owner_organization", None),
    )
    row["owner"] = {"kind": kind, "handle": handle, "organization_id": org} if kind else None
    return row


async def workspace_summary(db: AsyncSession, principal: Principal, workspace_id: UUID) -> dict[str, Any]:
    await set_context(db, workspace_id=workspace_id, user_id=principal.user_id)
    row = (
        await db.execute(
            text(
                "SELECT w.id, w.name, w.slug, m.role, n.kind AS owner_kind, n.handle AS owner_handle,"
                " n.organization_id AS owner_organization FROM workspaces w"
                " JOIN memberships m ON m.workspace_id = w.id AND m.user_id = :u"
                " LEFT JOIN namespaces n ON n.id = w.namespace_id WHERE w.id = :w"
            ),
            {"w": workspace_id, "u": principal.user_id},
        )
    ).one()
    return _with_owner(dict(row._mapping))


async def list_workspaces(db: AsyncSession, principal: Principal) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT w.id, w.name, w.slug, m.role, n.kind AS owner_kind, n.handle AS owner_handle,"
            " n.organization_id AS owner_organization"
            " FROM workspaces w JOIN memberships m ON m.workspace_id = w.id"
            " LEFT JOIN namespaces n ON n.id = w.namespace_id"
            " WHERE m.user_id = :u AND w.status = 'active' ORDER BY w.created_at, w.id"
        ),
        {"u": principal.user_id},
    )
    return [_with_owner(dict(r._mapping)) for r in rows]


async def get_workspace(db: AsyncSession, member: Member) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT w.id, w.name, w.slug, w.updated_at, n.kind AS owner_kind, n.handle AS owner_handle,"
                " n.organization_id AS owner_organization FROM workspaces w"
                " LEFT JOIN namespaces n ON n.id = w.namespace_id WHERE w.id = :w"
            ),
            {"w": member.workspace_id},
        )
    ).one()
    return _with_owner({**dict(row._mapping), "role": member.role})


async def rename_workspace(
    db: AsyncSession, member: Member, name: str, if_match: str | None
) -> dict[str, Any]:
    if if_match is None:
        raise ApiError(428, "PRECONDITION_REQUIRED", "Send If-Match with the workspace ETag.")
    current = (
        await db.execute(
            text("SELECT updated_at FROM workspaces WHERE id = :w FOR UPDATE"), {"w": member.workspace_id}
        )
    ).scalar_one()
    if etag(current) != if_match:
        raise ApiError(409, "VERSION_CONFLICT", "The workspace changed. Reload and try again.")
    await db.execute(
        text("UPDATE workspaces SET name = :n, updated_at = clock_timestamp() WHERE id = :w"),
        {"n": name, "w": member.workspace_id},
    )
    await audit(db, member, "workspace.renamed", "workspace", member.workspace_id)
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_workspace(db, member)


# ---------------------------------------------------------------- members
async def list_members(
    db: AsyncSession, member: Member, after: Position | None, limit: int
) -> tuple[list[dict[str, Any]], Position | None]:
    rows = (
        await db.execute(
            text(
                "SELECT m.id AS membership_id, m.user_id, u.display_name, m.role, m.created_at, m.via_organization"
                " FROM memberships m JOIN users u ON u.id = m.user_id"
                " WHERE m.workspace_id = :w AND (CAST(:t AS timestamptz) IS NULL"
                "        OR (m.created_at, m.id) > (CAST(:t AS timestamptz), :i))"
                " ORDER BY m.created_at, m.id LIMIT :lim"
            ),
            {
                "w": member.workspace_id,
                "t": after.created_at if after else None,
                "i": after.id if after else None,
                "lim": limit + 1,
            },
        )
    ).all()
    items = [dict(r._mapping) for r in rows[:limit]]
    last = rows[limit - 1] if len(rows) > limit else None
    return items, (Position(last.created_at, last.membership_id) if last else None)


async def _locked_target(db: AsyncSession, member: Member, user_id: UUID) -> str:
    # Lock the workspace first: role changes and removals serialize per workspace (owner invariant).
    await db.execute(text("SELECT 1 FROM workspaces WHERE id = :w FOR UPDATE"), {"w": member.workspace_id})
    row = (
        await db.execute(
            text(
                "SELECT role, via_organization FROM memberships WHERE workspace_id = :w AND user_id = :u FOR UPDATE"
            ),
            {"w": member.workspace_id, "u": user_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Member not found.")
    if row.via_organization:
        # 0038: an organization's owners and admins hold these roles in all its workspaces.
        raise ApiError(
            409, "MANAGED_BY_ORGANIZATION", "This role comes from the organization; change it there."
        )
    return str(row.role)


async def _owners(db: AsyncSession, workspace_id: UUID) -> int:
    count: int = (
        await db.execute(
            text("SELECT count(*) FROM memberships WHERE workspace_id = :w AND role = 'owner'"),
            {"w": workspace_id},
        )
    ).scalar_one()
    return count


async def change_role(db: AsyncSession, member: Member, user_id: UUID, new_role: Role) -> None:
    target_role = await _locked_target(db, member, user_id)
    if not can_manage(member, target_role, new_role):
        raise ApiError(403, "FORBIDDEN", "Your role does not allow this change.")
    if target_role == new_role:
        return
    if target_role == "owner" and await _owners(db, member.workspace_id) == 1:
        raise ApiError(409, "LAST_OWNER", "A workspace must keep at least one owner.")
    await db.execute(
        text("UPDATE memberships SET role = :r, updated_at = now() WHERE workspace_id = :w AND user_id = :u"),
        {"r": new_role, "w": member.workspace_id, "u": user_id},
    )
    await audit(
        db,
        member,
        "member.role_changed",
        "membership",
        None,
        {"user_id": str(user_id), "from": target_role, "to": new_role},
    )
    await commit_or_conflict(db)


async def remove_member(db: AsyncSession, member: Member, user_id: UUID) -> None:
    target_role = await _locked_target(db, member, user_id)
    leaving = user_id == member.user_id
    if not leaving and not can_manage(member, target_role):
        raise ApiError(403, "FORBIDDEN", "Your role does not allow removing this member.")
    if target_role == "owner" and await _owners(db, member.workspace_id) == 1:
        raise ApiError(409, "LAST_OWNER", "A workspace must keep at least one owner.")
    await db.execute(
        text("DELETE FROM memberships WHERE workspace_id = :w AND user_id = :u"),
        {"w": member.workspace_id, "u": user_id},
    )
    await tokens.revoke_for_member(db, member.workspace_id, user_id)
    await audit(
        db,
        member,
        "member.left" if leaving else "member.removed",
        "membership",
        None,
        {"user_id": str(user_id), "role": target_role},
    )
    await commit_or_conflict(db)


# ---------------------------------------------------------------- invitations
@dataclass(frozen=True)
class CreatedInvitation:
    id: UUID
    email: str
    role: Role
    expires_at: datetime
    token: str  # shown once to the inviter (no email delivery in M1); only its digest is stored


async def invite(db: AsyncSession, member: Member, email: str, role: Role) -> CreatedInvitation:
    if not can_invite(member, role):
        raise ApiError(403, "FORBIDDEN", "Your role does not allow inviting with this role.")
    normalized = normalize_email(email)
    token = new_token()
    try:
        row = (
            await db.execute(
                text(
                    "INSERT INTO invitations(workspace_id, email_normalized, role, token_hash, invited_by,"
                    " expires_at) VALUES (:w, :e, :r, :t, :u, now() + make_interval(secs => :life))"
                    " RETURNING id, expires_at"
                ),
                {
                    "w": member.workspace_id,
                    "e": normalized,
                    "r": role,
                    "t": digest(token),
                    "u": member.user_id,
                    "life": int(INVITATION_LIFETIME.total_seconds()),
                },
            )
        ).one()
    except exc.IntegrityError as error:
        await db.rollback()
        raise ApiError(409, "INVITATION_EXISTS", "This address already has a pending invitation.") from error
    await audit(db, member, "invitation.created", "invitation", row.id, {"role": role})
    await db.commit()
    return CreatedInvitation(row.id, normalized, role, row.expires_at, token)


async def list_invitations(db: AsyncSession, member: Member) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT id, email_normalized AS email, role, created_at, expires_at FROM invitations"
            " WHERE workspace_id = :w AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()"
            " ORDER BY created_at, id"
        ),
        {"w": member.workspace_id},
    )
    return [dict(r._mapping) for r in rows]


async def revoke_invitation(db: AsyncSession, member: Member, invitation_id: UUID) -> None:
    row = (
        await db.execute(
            text(
                "UPDATE invitations SET revoked_at = now() WHERE workspace_id = :w AND id = :i"
                " AND accepted_at IS NULL AND revoked_at IS NULL RETURNING role"
            ),
            {"w": member.workspace_id, "i": invitation_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Invitation not found.")
    if not can_invite(member, row.role):
        raise ApiError(403, "FORBIDDEN", "Your role does not allow revoking this invitation.")
    await audit(db, member, "invitation.revoked", "invitation", invitation_id)
    await db.commit()


async def accept_invitation(db: AsyncSession, principal: Principal, token: str) -> UUID:
    """Join a workspace. The invitee needs a verified address equal to the invited one (09)."""
    invalid = ApiError(404, "INVITATION_INVALID", "This invitation is invalid, expired or already used.")
    if not token or len(token) > 256:
        raise invalid
    found = (
        await db.execute(text("SELECT * FROM app_private.invitation_by_token(:t)"), {"t": digest(token)})
    ).one_or_none()
    if found is None:
        raise invalid
    verified = {
        r.verified_email
        for r in await db.execute(
            text(
                "SELECT verified_email FROM oauth_identities"
                " WHERE user_id = :u AND verified_email IS NOT NULL"
            ),
            {"u": principal.user_id},
        )
    }
    # The workspace comes from the validated token, never from request data.
    await set_context(db, workspace_id=found.workspace_id, user_id=principal.user_id)
    live = (
        await db.execute(
            text(
                "SELECT id FROM invitations WHERE workspace_id = :w AND id = :i AND accepted_at IS NULL"
                " AND revoked_at IS NULL AND expires_at > now() FOR UPDATE"
            ),
            {"w": found.workspace_id, "i": found.invitation_id},
        )
    ).one_or_none()
    if live is None:
        raise invalid
    if found.email_normalized not in verified:
        raise ApiError(
            403,
            "INVITATION_EMAIL_MISMATCH",
            "Sign in with an account whose verified email matches the invitation.",
        )
    existing = (
        await db.execute(
            text("SELECT role FROM memberships WHERE workspace_id = :w AND user_id = :u"),
            {"w": found.workspace_id, "u": principal.user_id},
        )
    ).scalar_one_or_none()
    if existing is None:
        await db.execute(
            text("INSERT INTO memberships(workspace_id, user_id, role) VALUES (:w, :u, :r)"),
            {"w": found.workspace_id, "u": principal.user_id, "r": found.role},
        )
    elif ROLE_RANK[found.role] > ROLE_RANK[existing]:
        await db.execute(
            text(
                "UPDATE memberships SET role = :r, updated_at = now()"
                " WHERE workspace_id = :w AND user_id = :u"
            ),
            {"r": found.role, "w": found.workspace_id, "u": principal.user_id},
        )
    await db.execute(
        text(
            "UPDATE invitations SET accepted_at = now(), accepted_by = :u WHERE workspace_id = :w AND id = :i"
        ),
        {"u": principal.user_id, "w": found.workspace_id, "i": found.invitation_id},
    )
    member = Member(principal, found.workspace_id, found.role)
    await audit(db, member, "invitation.accepted", "invitation", found.invitation_id, {"role": found.role})
    await db.commit()
    workspace_id: UUID = found.workspace_id
    return workspace_id
