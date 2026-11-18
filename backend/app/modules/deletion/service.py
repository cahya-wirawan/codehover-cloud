"""Project, workspace and account deletion, and the account data export (T17; 01 FR-021, 03, 08, A22).

Every deletion works the same way, in one transaction:
1. Stop delivery: the target becomes invisible to every read path (`projects.deleted_at`, workspace status
   `deleting`, user status `deleted`). Resolvers, membership checks and sessions already filter on these.
2. Fence work: live builds end as cancelled with their unspent envelope released, and every unfinished
   task is cancelled, so a worker holding its lease stops at the next heartbeat and commits nothing.
3. Record the tombstone (`deletion_requests`), with identifiers only, plus an audit event.

After the commit, the tombstone is also written to object storage, where a database restored from an
older backup finds it again (`app.ops.tombstones`). The purge (`app.jobs.purge`) removes content later.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.request_context import current_request_id
from app.core.tenancy import set_context
from app.engine.keys import tombstone_key
from app.jobs import store as jobs
from app.modules.identity.sessions import Principal
from app.modules.workspaces.access import Member, require_role
from app.modules.workspaces.service import audit

logger = logging.getLogger("app.deletion")
ACCOUNT_CONFIRMATION = "delete my account"


@dataclass(frozen=True)
class Tombstone:
    id: UUID
    scope: str
    workspace_id: UUID | None
    project_id: UUID | None
    user_id: UUID | None
    requested_at: datetime
    not_before: datetime

    def document(self) -> bytes:
        """The object-storage copy: identifiers and times only."""
        return json.dumps(
            {
                "schema": "urn:codehover-cloud:schema:tombstone:1.0.0",
                "id": str(self.id),
                "scope": self.scope,
                "workspace_id": str(self.workspace_id) if self.workspace_id else None,
                "project_id": str(self.project_id) if self.project_id else None,
                "user_id": str(self.user_id) if self.user_id else None,
                "requested_at": self.requested_at.isoformat(),
            },
            sort_keys=True,
        ).encode()


async def record_tombstone(store: ObjectStore, tombstone: Tombstone) -> None:
    """After the commit. A failure is logged and repaired by the purge, which writes it again first."""
    try:
        await store.put(tombstone_key(tombstone.id), tombstone.document(), "application/json")
    except Exception:
        logger.exception("tombstone copy not stored yet", extra={"deletion_id": str(tombstone.id)})


async def _insert(
    db: AsyncSession,
    scope: str,
    requested_by: UUID,
    workspace_id: UUID | None = None,
    project_id: UUID | None = None,
    user_id: UUID | None = None,
) -> Tombstone:
    row = (
        await db.execute(
            text(
                "INSERT INTO deletion_requests(scope, workspace_id, project_id, user_id, requested_by)"
                " VALUES (:s, :w, :p, :u, :by) RETURNING id, requested_at, not_before"
            ),
            {"s": scope, "w": workspace_id, "p": project_id, "u": user_id, "by": requested_by},
        )
    ).one()
    return Tombstone(row.id, scope, workspace_id, project_id, user_id, row.requested_at, row.not_before)


# ---------------------------------------------------------------- project
async def delete_project(db: AsyncSession, member: Member, project_id: UUID, confirmation: str) -> Tombstone:
    require_role(member, "admin")
    project = (
        await db.execute(
            text(
                "SELECT name FROM projects WHERE workspace_id = :w AND id = :p AND deleted_at IS NULL FOR UPDATE"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if project is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    if confirmation != project.name:
        raise ApiError(
            422,
            "CONFIRMATION_MISMATCH",
            "Type the project name exactly to confirm.",
            details={"field": "confirmation"},
        )
    await db.execute(
        text(
            "UPDATE projects SET deleted_at = now(), updated_at = now() WHERE workspace_id = :w AND id = :p"
        ),
        {"w": member.workspace_id, "p": project_id},
    )
    fenced = await jobs.fence_project(db, member.workspace_id, project_id)
    tombstone = await _insert(
        db, "project", member.user_id, workspace_id=member.workspace_id, project_id=project_id
    )
    await audit(db, member, "project.deleted", "project", project_id, {"tasks_fenced": fenced})
    await db.commit()
    return tombstone


# ---------------------------------------------------------------- workspace
async def _delete_workspace(db: AsyncSession, member: Member) -> Tombstone:
    subscribed = (
        await db.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM subscriptions WHERE workspace_id = :w"
                " AND status IN ('trialing', 'active', 'past_due', 'unpaid', 'paused', 'incomplete'))"
            ),
            {"w": member.workspace_id},
        )
    ).scalar_one()
    if subscribed:  # 11: deleting never silently keeps charging; cancel in the portal first
        raise ApiError(
            409,
            "SUBSCRIPTION_ACTIVE",
            "This workspace has a subscription. Cancel it in billing before deleting the workspace.",
        )
    await db.execute(
        text(
            "UPDATE workspaces SET status = 'deleting', deleted_at = now(), updated_at = now() WHERE id = :w"
        ),
        {"w": member.workspace_id},
    )
    projects = (
        await db.execute(
            text(
                "UPDATE projects SET deleted_at = now(), updated_at = now()"
                " WHERE workspace_id = :w AND deleted_at IS NULL RETURNING id"
            ),
            {"w": member.workspace_id},
        )
    ).scalars()
    fenced = 0
    for project_id in list(projects):
        fenced += await jobs.fence_project(db, member.workspace_id, project_id)
    await db.execute(
        text("DELETE FROM invitations WHERE workspace_id = :w AND accepted_at IS NULL"),
        {"w": member.workspace_id},
    )
    tombstone = await _insert(db, "workspace", member.user_id, workspace_id=member.workspace_id)
    await audit(db, member, "workspace.deleted", "workspace", member.workspace_id, {"tasks_fenced": fenced})
    return tombstone


async def delete_workspace(db: AsyncSession, member: Member, confirmation: str) -> Tombstone:
    require_role(member, "owner")
    workspace = (
        await db.execute(
            text("SELECT slug FROM workspaces WHERE id = :w AND status = 'active' FOR UPDATE"),
            {"w": member.workspace_id},
        )
    ).one_or_none()
    if workspace is None:
        raise ApiError(404, "NOT_FOUND", "Workspace not found.")
    if confirmation != workspace.slug:
        raise ApiError(
            422,
            "CONFIRMATION_MISMATCH",
            "Type the workspace address exactly to confirm.",
            details={"field": "confirmation"},
        )
    tombstone = await _delete_workspace(db, member)
    await db.commit()
    return tombstone


# ---------------------------------------------------------------- account
@dataclass(frozen=True)
class AccountDeletion:
    account: Tombstone
    workspaces: list[Tombstone]


async def _memberships(db: AsyncSession, user_id: UUID) -> list[Any]:
    await set_context(db, user_id=user_id)
    return list(
        (
            await db.execute(
                text(
                    "SELECT m.workspace_id, m.role, w.slug, w.name FROM memberships m"
                    " JOIN workspaces w ON w.id = m.workspace_id"
                    " WHERE m.user_id = :u AND w.status = 'active' ORDER BY w.created_at, w.id"
                ),
                {"u": user_id},
            )
        ).all()
    )


async def _owner_count(db: AsyncSession, workspace_id: UUID, user_id: UUID) -> int:
    await set_context(db, workspace_id=workspace_id, user_id=user_id)
    count: int = (
        await db.execute(
            text("SELECT count(*) FROM memberships WHERE workspace_id = :w AND role = 'owner'"),
            {"w": workspace_id},
        )
    ).scalar_one()
    return count


async def delete_account(
    db: AsyncSession, principal: Principal, confirmation: str, delete_workspaces: list[UUID]
) -> AccountDeletion:
    """Self only, within 10 minutes of signing in (09). Every workspace the user owns alone must be listed
    for deletion (or get another owner first); the user leaves every other workspace (03)."""
    if not principal.recently_authenticated():
        raise ApiError(403, "REAUTH_REQUIRED", "Sign in again to delete your account.")
    if confirmation != ACCOUNT_CONFIRMATION:
        raise ApiError(
            422,
            "CONFIRMATION_MISMATCH",
            f'Type "{ACCOUNT_CONFIRMATION}" to confirm.',
            details={"field": "confirmation"},
        )
    user_id = principal.user_id
    memberships = await _memberships(db, user_id)
    sole: dict[UUID, Any] = {}
    for m in memberships:
        if m.role == "owner" and await _owner_count(db, m.workspace_id, user_id) == 1:
            sole[m.workspace_id] = m
    listed = set(delete_workspaces)
    if listed - set(sole):
        raise ApiError(
            422,
            "NOT_SOLE_OWNER",
            "Only workspaces you own alone can be deleted with your account.",
            details={"field": "delete_workspaces"},
        )
    unresolved = [w for w in sole if w not in listed]
    if unresolved:
        raise ApiError(
            409,
            "OWNERSHIP_TRANSFER_REQUIRED",
            "Give these workspaces another owner, or delete them with your account.",
            details={
                "workspaces": [{"id": str(w), "slug": sole[w].slug, "name": sole[w].name} for w in unresolved]
            },
        )
    removed: list[Tombstone] = []
    for m in memberships:
        await set_context(db, workspace_id=m.workspace_id, user_id=user_id)
        member = Member(principal, m.workspace_id, m.role)
        if m.workspace_id in listed:
            await db.execute(text("SELECT 1 FROM workspaces WHERE id = :w FOR UPDATE"), {"w": m.workspace_id})
            removed.append(await _delete_workspace(db, member))
        else:
            await db.execute(
                text("DELETE FROM memberships WHERE workspace_id = :w AND user_id = :u"),
                {"w": m.workspace_id, "u": user_id},
            )
            await audit(
                db, member, "member.left", "membership", None, {"user_id": str(user_id), "role": m.role}
            )
    await set_context(db, user_id=user_id)
    # Sessions end now; identities are removed, so signing in again starts a new, empty account.
    await db.execute(
        text(
            "UPDATE users SET status = 'deleted', deleted_at = now(), display_name = 'Deleted user',"
            " primary_email = NULL, updated_at = now() WHERE id = :u"
        ),
        {"u": user_id},
    )
    await db.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"),
        {"u": user_id},
    )
    await db.execute(text("DELETE FROM oauth_identities WHERE user_id = :u"), {"u": user_id})
    account = await _insert(db, "account", user_id, user_id=user_id)
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata,"
            " request_id) VALUES (NULL, :u, 'account.deleted', 'user', :u, CAST(:m AS jsonb), :req)"
        ),
        {"u": user_id, "m": json.dumps({"workspaces_deleted": len(removed)}), "req": current_request_id()},
    )
    await db.commit()
    return AccountDeletion(account, removed)


# ---------------------------------------------------------------- account data export
async def export_account(db: AsyncSession, principal: Principal) -> dict[str, Any]:
    """Profile, sign-in methods, memberships, projects and the user's own builds (08). Other members'
    account details are never included; source and explanations come through build exports."""
    if not principal.recently_authenticated():
        raise ApiError(403, "REAUTH_REQUIRED", "Sign in again to export your account data.")
    user_id = principal.user_id
    await set_context(db, user_id=user_id)
    profile = (
        await db.execute(
            text("SELECT id, display_name, primary_email, created_at FROM users WHERE id = :u"),
            {"u": user_id},
        )
    ).one()
    identities = (
        await db.execute(
            text(
                "SELECT provider, verified_email, created_at FROM oauth_identities WHERE user_id = :u"
                " ORDER BY created_at"
            ),
            {"u": user_id},
        )
    ).all()
    workspaces = []
    for m in await _memberships(db, user_id):
        await set_context(db, workspace_id=m.workspace_id, user_id=user_id)
        projects = (
            await db.execute(
                text(
                    "SELECT p.id, p.name, p.slug, p.tracked_ref, r.owner || '/' || r.name AS repository, p.created_at"
                    " FROM projects p JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                    " WHERE p.workspace_id = :w AND p.deleted_at IS NULL ORDER BY p.created_at"
                ),
                {"w": m.workspace_id},
            )
        ).all()
        builds = (
            await db.execute(
                text(
                    "SELECT b.id, b.project_id, b.status, b.created_at, b.completed_at FROM builds b"
                    " JOIN projects p ON p.workspace_id = b.workspace_id AND p.id = b.project_id"
                    " WHERE b.workspace_id = :w AND b.created_by = :u AND p.deleted_at IS NULL ORDER BY b.created_at"
                ),
                {"w": m.workspace_id, "u": user_id},
            )
        ).all()
        workspaces.append(
            {
                "id": str(m.workspace_id),
                "name": m.name,
                "slug": m.slug,
                "role": m.role,
                "projects": [
                    {
                        "id": str(p.id),
                        "name": p.name,
                        "slug": p.slug,
                        "tracked_ref": p.tracked_ref,
                        "repository": p.repository,
                        "created_at": p.created_at.isoformat(),
                    }
                    for p in projects
                ],
                "builds_requested": [
                    {
                        "id": str(b.id),
                        "project_id": str(b.project_id),
                        "status": b.status,
                        "created_at": b.created_at.isoformat(),
                        "completed_at": b.completed_at.isoformat() if b.completed_at else None,
                    }
                    for b in builds
                ],
            }
        )
    await set_context(db, user_id=user_id)
    await db.execute(
        text(
            "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata,"
            " request_id) VALUES (NULL, :u, 'account.exported', 'user', :u, '{}'::jsonb, :req)"
        ),
        {"u": user_id, "req": current_request_id()},
    )
    await db.commit()
    return {
        "schema": "urn:codehover-cloud:schema:account-export:1.0.0",
        "profile": {
            "id": str(profile.id),
            "display_name": profile.display_name,
            "primary_email": profile.primary_email,
            "created_at": profile.created_at.isoformat(),
        },
        "sign_in_methods": [
            {
                "provider": i.provider,
                "verified_email": i.verified_email,
                "created_at": i.created_at.isoformat(),
            }
            for i in identities
        ],
        "workspaces": workspaces,
    }
