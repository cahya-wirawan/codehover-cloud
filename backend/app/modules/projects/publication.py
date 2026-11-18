"""Publication: the pointer from a project to one reviewed, ready build (06, 01 FR-016/FR-017, A15).

Publishing is an admin action with optimistic concurrency: the current pointer's ETag must be sent in
`If-Match`, so a concurrent change is a conflict the user inspects, never a silent overwrite. The build
must be ready, belong to the project, pass the quality gate (partly supported claims need an explicit
acknowledgment), and the repository authority must still be valid. Private is the default (an optional
slug gives members a site address); public needs a slug and the project name typed as confirmation. Rollback is publishing an older ready build. Every
change is audited. A suspended publication (revoked access, 0010 trigger) stops serving until an admin
publishes again.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.modules.projects import quality
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


def etag(version: int) -> str:
    return f'"publication-{version}"'


async def current(db: AsyncSession, member: Member, project_id: UUID) -> dict[str, Any] | None:
    row = (
        await db.execute(
            text(
                "SELECT p.build_id, p.visibility, p.public_slug, p.version, p.acknowledged_partial, p.suspended_at,"
                " p.suspended_reason, p.updated_at, s.commit_sha FROM publications p"
                " JOIN builds b ON b.workspace_id = p.workspace_id AND b.id = p.build_id"
                " JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
                " WHERE p.workspace_id = :w AND p.project_id = :p"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    return None if row is None else dict(row._mapping)


async def _check_precondition(existing: Any, if_match: str | None) -> None:
    if existing is None:
        if if_match not in (None, "*"):
            raise ApiError(
                409, "VERSION_CONFLICT", "The project is not published any more.", details={"version": None}
            )
        return
    if if_match is None:
        raise ApiError(428, "PRECONDITION_REQUIRED", "Send If-Match with the current publication ETag.")
    if if_match != etag(existing.version):
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            "The publication changed; review the current state first.",
            details={"version": existing.version},
        )


async def publish(
    db: AsyncSession,
    objects: ObjectStore,
    member: Member,
    project_id: UUID,
    *,
    build_id: UUID,
    visibility: str,
    public_slug: str | None,
    confirmation: str | None,
    acknowledge_partial: bool,
    if_match: str | None,
) -> dict[str, Any]:
    target = (
        await db.execute(
            text(
                "SELECT status, estimate_id FROM builds WHERE workspace_id = :w AND project_id = :p AND id = :b"
            ),
            {"w": member.workspace_id, "p": project_id, "b": build_id},
        )
    ).one_or_none()
    if target is None:
        raise ApiError(404, "NOT_FOUND", "Build not found in this project.")
    if target.status != "ready":
        raise ApiError(409, "BUILD_NOT_READY", "Only a ready build can be published.")
    report = await quality.summary(db, objects, member, build_id)  # commits; locks are taken below
    if report["blockers"]:
        raise ApiError(
            409,
            "PUBLICATION_BLOCKED",
            "This build does not pass the review gate.",
            details={"blockers": report["blockers"]},
        )
    if report["requires_acknowledgment"] and not acknowledge_partial:
        raise ApiError(
            422, "ACKNOWLEDGMENT_REQUIRED", "Acknowledge the partly supported claims before publishing."
        )

    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    project = (
        await db.execute(
            text(
                "SELECT p.name, r.access_revoked_at, c.status FROM projects p"
                " JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                # FOR SHARE OF r, c: a concurrent revoke waits for this publish, and its trigger then suspends
                # the new pointer; a revoke that committed first is seen here and refused.
                " WHERE p.workspace_id = :w AND p.id = :p AND p.deleted_at IS NULL FOR UPDATE OF p FOR SHARE OF r, c"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if project is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    existing = (
        await db.execute(
            text(
                "SELECT build_id, visibility, version, suspended_reason FROM publications"
                " WHERE workspace_id = :w AND project_id = :p FOR UPDATE"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    await _check_precondition(existing, if_match)
    if existing is not None and existing.suspended_reason == "operator":
        # Only access-related suspensions are cleared by republishing; an operator lifts their own.
        raise ApiError(409, "SUSPENDED_BY_OPERATOR", "This site was suspended by an operator.")
    if project.status != "active" or project.access_revoked_at is not None:
        raise ApiError(409, "CONNECTION_INACTIVE", "Reconnect GitHub before publishing this project.")
    still_ready = (
        await db.execute(
            text("SELECT status FROM builds WHERE workspace_id = :w AND id = :b FOR SHARE"),
            {"w": member.workspace_id, "b": build_id},
        )
    ).scalar_one()
    if still_ready != "ready":
        raise ApiError(409, "BUILD_NOT_READY", "Only a ready build can be published.")
    # A slug gives the site an address. Public sites need one and the typed confirmation; a private site may
    # have one too, readable only by signed-in members (02 S20).
    if public_slug is not None and not SLUG.fullmatch(public_slug):
        raise ApiError(422, "SLUG_INVALID", "Choose an address of 3-63 lowercase letters, digits or hyphens.")
    if visibility == "public":
        if public_slug is None:
            raise ApiError(422, "SLUG_INVALID", "A public site needs an address.")
        if confirmation != project.name:
            raise ApiError(
                422, "CONFIRMATION_REQUIRED", "Type the project name to confirm public disclosure."
            )
    slug = public_slug
    params = {
        "w": member.workspace_id,
        "p": project_id,
        "b": build_id,
        "v": visibility,
        "s": slug,
        "a": bool(acknowledge_partial and report["requires_acknowledgment"]),
        "u": member.user_id,
    }
    try:
        if existing is None:
            await db.execute(
                text(
                    "INSERT INTO publications(workspace_id, project_id, build_id, visibility, public_slug,"
                    " acknowledged_partial, published_by) VALUES (:w, :p, :b, :v, :s, :a, :u)"
                ),
                params,
            )
        else:
            await db.execute(
                text(
                    "UPDATE publications SET build_id = :b, visibility = :v, public_slug = :s, acknowledged_partial = :a,"
                    " published_by = :u, version = version + 1, suspended_at = NULL, suspended_reason = NULL,"
                    " updated_at = now() WHERE workspace_id = :w AND project_id = :p"
                ),
                params,
            )
    except exc.IntegrityError as error:
        await db.rollback()
        raise ApiError(409, "SLUG_TAKEN", "That public address is already in use.") from error
    await audit(
        db,
        member,
        "publication.changed",
        "project",
        project_id,
        {
            "build_id": str(build_id),
            "visibility": visibility,
            "public_slug": slug,
            "previous_build_id": str(existing.build_id) if existing else None,
            "previous_visibility": existing.visibility if existing else None,
        },
    )
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    result = await current(db, member, project_id)
    assert result is not None
    return result


async def unpublish(db: AsyncSession, member: Member, project_id: UUID, if_match: str | None) -> None:
    existing = (
        await db.execute(
            text(
                "SELECT build_id, visibility, version FROM publications WHERE workspace_id = :w AND project_id = :p FOR UPDATE"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if existing is None:
        raise ApiError(404, "NOT_PUBLISHED", "This project is not published.")
    await _check_precondition(existing, if_match)
    await db.execute(
        text("DELETE FROM publications WHERE workspace_id = :w AND project_id = :p"),
        {"w": member.workspace_id, "p": project_id},
    )
    await audit(
        db,
        member,
        "publication.removed",
        "project",
        project_id,
        {"build_id": str(existing.build_id), "visibility": existing.visibility},
    )
    await db.commit()
