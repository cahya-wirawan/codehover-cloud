"""Publication: the pointer from a project to one reviewed, ready build (06, 01 FR-016/FR-017, A15).

Publishing is an admin action with optimistic concurrency: the current pointer's ETag must be sent in
`If-Match`, so a concurrent change is a conflict the user inspects, never a silent overwrite. The build
must be ready, belong to the project, pass the quality gate (partly supported claims need an explicit
acknowledgment), and the repository authority must still be valid. Private is the default (an optional
slug gives members a site address); public needs a slug and the project name typed as confirmation; a public site of a repository read through the public source
(FR-028) also needs a license that permits redistribution at the build's commit (FR-029, `licenses`).
Rollback is publishing an older ready build. Every
change is audited. A suspended publication (revoked access, 0010 trigger) stops serving until an admin
publishes again.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.github.app import GitHubError
from app.adapters.github.public import License, PublicGitHub
from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.modules.billing import service as billing
from app.modules.projects import licenses, quality
from app.modules.projects.estimates import public_unavailable
from app.modules.projects.service import TEXT_STATUSES
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
                " p.suspended_reason, p.updated_at, p.license_spdx, s.commit_sha FROM publications p"
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


def _not_permitted(reason: str, spdx_id: str | None, message: str) -> ApiError:
    return ApiError(409, "LICENSE_NOT_PERMITTED", message, details={"reason": reason, "license": spdx_id})


async def _public_license(
    db: AsyncSession, client: PublicGitHub | None, member: Member, build_id: UUID
) -> License | None:
    """For a repository read through the public source (FR-028), the license that permits a public site of
    this build's commit (FR-029), or an error saying why there is none. None for App-granted or uploaded
    repositories, which are not checked. Opens no transaction across the GitHub call."""
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    source = (
        await db.execute(
            text(
                "SELECT c.provider, r.provider_repo_id, s.id AS snapshot_id, s.commit_sha FROM builds b"
                " JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
                " JOIN projects p ON p.workspace_id = b.workspace_id AND p.id = b.project_id"
                " JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE b.workspace_id = :w AND b.id = :b"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    ).one()
    await db.commit()
    if source.provider != "public":
        return None
    if client is None:
        raise ApiError(503, "UNAVAILABLE", "GitHub is not reachable right now.", retryable=True)
    try:
        found = await client.license(source.provider_repo_id, source.commit_sha)
    except GitHubError as error:
        if error.reason == "not_found":
            raise ApiError(
                409, "REPOSITORY_NOT_PUBLIC", "This repository is no longer public on GitHub."
            ) from error
        raise public_unavailable(error) from error
    if found is None:
        raise _not_permitted(
            "no_license",
            None,
            "This repository has no license at this commit, so its code may not be shared publicly."
            " Publish the site privately instead.",
        )
    if found.spdx_id == "NOASSERTION":
        raise _not_permitted(
            "unrecognized",
            None,
            f"GitHub could not identify the license in {found.path}, so a public site is not allowed."
            " Publish the site privately instead.",
        )
    if not licenses.permits_public_site(found.spdx_id):
        raise _not_permitted(
            "not_permitted",
            found.spdx_id,
            f"The {found.spdx_id} license of this repository does not permit a public site."
            " Publish the site privately instead.",
        )
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    carried = (
        await db.execute(
            text(
                "SELECT 1 FROM source_files WHERE workspace_id = :w AND snapshot_id = :s AND path = :path"
                " AND blob_key IS NOT NULL AND scan_status = ANY(:statuses)"
            ),
            {
                "w": member.workspace_id,
                "s": source.snapshot_id,
                "path": found.path,
                "statuses": list(TEXT_STATUSES),
            },
        )
    ).one_or_none()
    await db.commit()
    if carried is None:  # the site must carry the license notice the license requires
        raise _not_permitted(
            "license_file_missing",
            found.spdx_id,
            f"The license file {found.path} is not part of this build, so the site could not show it.",
        )
    return found


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
    billing_enforced: bool = False,
    public_github: PublicGitHub | None = None,
) -> dict[str, Any]:
    target = (
        await db.execute(
            text(
                "SELECT status, estimate_id, content_purged_at FROM builds"
                " WHERE workspace_id = :w AND project_id = :p AND id = :b"
            ),
            {"w": member.workspace_id, "p": project_id, "b": build_id},
        )
    ).one_or_none()
    if target is None:
        raise ApiError(404, "NOT_FOUND", "Build not found in this project.")
    if target.status != "ready":
        raise ApiError(409, "BUILD_NOT_READY", "Only a ready build can be published.")
    if target.content_purged_at is not None:
        raise ApiError(
            409, "BUILD_EXPIRED", "Retention removed this build's content; publish or export a newer build."
        )
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
    checked = None
    if visibility == "public":  # before any lock: the license check may call GitHub (FR-029)
        checked = await _public_license(db, public_github, member, build_id)

    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    project = (
        await db.execute(
            text(
                "SELECT p.name, r.access_revoked_at, c.status, c.provider FROM projects p"
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
    hosting = await billing.state(db, member.workspace_id, enforced=billing_enforced)
    if hosting.hosting_suspended:
        # Only the billing sync lifts a billing suspension; republishing must not (security review).
        raise ApiError(
            409,
            "SUSPENDED_FOR_BILLING",
            "Sites are offline until the plan is in good standing. Reading and export continue.",
        )
    # FOR SHARE also orders this against the retention sweep, which locks the build before purging it.
    still_ready = (
        await db.execute(
            text(
                "SELECT status, content_purged_at FROM builds WHERE workspace_id = :w AND id = :b FOR SHARE"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    ).one()
    if still_ready.status != "ready":
        raise ApiError(409, "BUILD_NOT_READY", "Only a ready build can be published.")
    if still_ready.content_purged_at is not None:
        raise ApiError(
            409, "BUILD_EXPIRED", "Retention removed this build's content; publish or export a newer build."
        )
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
        if project.provider == "public" and checked is None:
            # The repository became public-source between the check and the lock; check again.
            raise ApiError(409, "VERSION_CONFLICT", "The project changed; publish again.", retryable=True)
    if project.provider != "public" or visibility != "public":
        checked = None  # recorded only for what was checked: public sites of public-source repositories
    slug = public_slug
    params = {
        "w": member.workspace_id,
        "p": project_id,
        "b": build_id,
        "v": visibility,
        "s": slug,
        "a": bool(acknowledge_partial and report["requires_acknowledgment"]),
        "u": member.user_id,
        "ls": checked.spdx_id if checked else None,
        "lp": checked.path if checked else None,
    }
    try:
        if existing is None:
            await db.execute(
                text(
                    "INSERT INTO publications(workspace_id, project_id, build_id, visibility, public_slug,"
                    " acknowledged_partial, published_by, license_spdx, license_path)"
                    " VALUES (:w, :p, :b, :v, :s, :a, :u, :ls, :lp)"
                ),
                params,
            )
        else:
            await db.execute(
                text(
                    "UPDATE publications SET build_id = :b, visibility = :v, public_slug = :s, acknowledged_partial = :a,"
                    " published_by = :u, license_spdx = :ls, license_path = :lp, version = version + 1,"
                    " suspended_at = NULL, suspended_reason = NULL, updated_at = now()"
                    " WHERE workspace_id = :w AND project_id = :p"
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
            "license": checked.spdx_id if checked else None,
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
                "SELECT build_id, visibility, version, suspended_reason FROM publications"
                " WHERE workspace_id = :w AND project_id = :p FOR UPDATE"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if existing is None:
        raise ApiError(404, "NOT_PUBLISHED", "This project is not published.")
    await _check_precondition(existing, if_match)
    if existing.suspended_reason == "operator":
        # The takedown lives on this row; removing it would let a new publication skip it (0012).
        raise ApiError(409, "SUSPENDED_BY_OPERATOR", "This site was suspended by an operator.")
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
