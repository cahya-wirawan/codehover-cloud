"""Snapshots before builds and preflight estimates (06 "/projects/{pid}/snapshots", "/estimates"; 01 FR-007).

A snapshot is resolved to an exact commit and acquired by a worker task; the estimate is computed from
its scan and the approved catalog, stored immutably with its config digest and price revision, and
expires after 30 minutes. Starting a generation build later requires that exact estimate (A05).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.github.app import GitHubApp, GitHubError
from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.engine import ENGINE_VERSION, GENERATION_PROMPT_VERSION
from app.engine.catalog import Catalog
from app.engine.estimate import VALIDITY_SECONDS, GenerationConfig, ScannedFile, config_digest, estimate
from app.jobs import store as jobs
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

DISCLAIMER = "Illustrative estimate; actual usage is metered and never exceeds the build cap."


async def _source(db: AsyncSession, member: Member, project_id: UUID) -> Any:
    source = (
        await db.execute(
            text(
                "SELECT p.tracked_ref, r.provider_repo_id, r.access_revoked_at, c.installation_id, c.status"
                " FROM projects p JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
                " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
                " WHERE p.workspace_id = :w AND p.id = :p AND p.deleted_at IS NULL"
            ),
            {"w": member.workspace_id, "p": project_id},
        )
    ).one_or_none()
    if source is None:
        raise ApiError(404, "NOT_FOUND", "Project not found.")
    return source


async def resolve_commit(
    db: AsyncSession, github: GitHubApp | None, member: Member, project_id: UUID, ref: str | None
) -> tuple[str, str]:
    """(requested ref, exact commit SHA). Commits first: no transaction is open during the GitHub call."""
    source = await _source(db, member, project_id)
    await db.commit()
    if source.status != "active" or source.access_revoked_at is not None:
        raise ApiError(409, "CONNECTION_INACTIVE", "Reconnect GitHub to build this project.")
    if github is None:
        raise ApiError(503, "GITHUB_APP_UNAVAILABLE", "GitHub repository access is not configured.")
    requested = ref or source.tracked_ref
    try:
        sha = await github.resolve_ref(source.installation_id, source.provider_repo_id, requested)
    except GitHubError as error:
        if error.status in (404, 422):
            raise ApiError(422, "REF_NOT_FOUND", "That branch, tag or commit does not exist.") from error
        raise ApiError(503, "UNAVAILABLE", "GitHub is not reachable right now.", retryable=True) from error
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return requested, sha


async def upsert_snapshot(db: AsyncSession, member: Member, project_id: UUID, sha: str, ref: str) -> Any:
    return (
        await db.execute(
            text(
                "INSERT INTO snapshots(workspace_id, project_id, commit_sha, observed_ref)"
                " VALUES (:w, :p, :sha, :ref) ON CONFLICT (project_id, commit_sha)"
                " DO UPDATE SET updated_at = now() RETURNING id, status"
            ),
            {"w": member.workspace_id, "p": project_id, "sha": sha, "ref": ref},
        )
    ).one()


async def request_snapshot(
    db: AsyncSession, github: GitHubApp | None, member: Member, project_id: UUID, ref: str | None
) -> dict[str, Any]:
    requested, sha = await resolve_commit(db, github, member, project_id, ref)
    snapshot = await upsert_snapshot(db, member, project_id, sha, requested)
    if snapshot.status in ("pending", "acquiring", "failed"):  # rejected and scanned are final
        await jobs.create_snapshot_task(db, member.workspace_id, snapshot.id)
        if snapshot.status == "failed":
            await db.execute(
                text(
                    "UPDATE snapshots SET status = 'pending', rejection_code = NULL, updated_at = now()"
                    " WHERE workspace_id = :w AND id = :s AND status = 'failed'"
                ),
                {"w": member.workspace_id, "s": snapshot.id},
            )
    await audit(db, member, "snapshot.requested", "snapshot", snapshot.id, {"commit_sha": sha})
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_snapshot(db, member, project_id, snapshot.id)


async def get_snapshot(
    db: AsyncSession, member: Member, project_id: UUID, snapshot_id: UUID
) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT id, project_id, commit_sha, observed_ref, status, rejection_code, file_count, source_bytes,"
                " created_at FROM snapshots WHERE workspace_id = :w AND project_id = :p AND id = :s"
            ),
            {"w": member.workspace_id, "p": project_id, "s": snapshot_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Snapshot not found.")
    counts = await db.execute(
        text(
            "SELECT scan_status, count(*) FROM source_files WHERE workspace_id = :w AND snapshot_id = :s"
            " GROUP BY scan_status ORDER BY scan_status"
        ),
        {"w": member.workspace_id, "s": snapshot_id},
    )
    return {**dict(row._mapping), "scan_summary": {status: n for status, n in counts}}


async def list_snapshot_files(
    db: AsyncSession,
    member: Member,
    project_id: UUID,
    snapshot_id: UUID,
    status: str | None,
    after: str,
    limit: int,
) -> list[dict[str, Any]]:
    await get_snapshot(db, member, project_id, snapshot_id)
    rows = await db.execute(
        text(
            "SELECT id, path, language, scan_status AS status, scan_reason AS reason, byte_count, line_count"
            " FROM source_files WHERE workspace_id = :w AND snapshot_id = :s"
            ' AND (CAST(:status AS text) IS NULL OR scan_status = :status) AND path COLLATE "C" > :after'
            ' ORDER BY path COLLATE "C" LIMIT :n'
        ),
        {"w": member.workspace_id, "s": snapshot_id, "status": status, "after": after, "n": limit},
    )
    return [dict(r._mapping) for r in rows]


async def scanned_files(db: AsyncSession, workspace_id: UUID, snapshot_id: UUID) -> list[ScannedFile]:
    rows = await db.execute(
        text(
            "SELECT id, path, scan_status, byte_count, COALESCE(line_count, 0) FROM source_files"
            " WHERE workspace_id = :w AND snapshot_id = :s ORDER BY path"
        ),
        {"w": workspace_id, "s": snapshot_id},
    )
    return [ScannedFile(str(r[0]), r[1], r[2], int(r[3]), int(r[4])) for r in rows]


async def create_estimate(
    db: AsyncSession,
    catalog: Catalog,
    member: Member,
    project_id: UUID,
    snapshot_id: UUID,
    config: GenerationConfig,
) -> dict[str, Any]:
    await _source(db, member, project_id)
    snapshot = await get_snapshot(db, member, project_id, snapshot_id)
    if snapshot["status"] != "scanned":
        raise ApiError(
            409,
            "SNAPSHOT_NOT_READY",
            "This snapshot has not finished scanning.",
            details={"status": snapshot["status"]},
        )
    preset = catalog.preset(config.model_preset)
    if preset is None:
        raise ApiError(422, "PRESET_UNKNOWN", "This model preset is not in the approved catalog.")
    result = estimate(await scanned_files(db, member.workspace_id, snapshot_id), config, catalog, preset)
    if not result.selected:
        raise ApiError(422, "SCOPE_EMPTY", "No supported source files match this scope.")
    digest = config_digest(
        config,
        commit_sha=snapshot["commit_sha"],
        revision=catalog.revision,
        engine=ENGINE_VERSION,
        prompts=GENERATION_PROMPT_VERSION,
    )
    tokens = {**result.tokens, "files": {"selected": len(result.selected), "excluded": result.excluded}}
    estimate_id = (
        await db.execute(
            text(
                "INSERT INTO estimates(workspace_id, project_id, snapshot_id, config, config_digest, price_version,"
                " low_micro_usd, high_micro_usd, token_estimate, expires_at, created_by)"
                " VALUES (:w, :p, :s, CAST(:c AS jsonb), :d, :v, :low, :high, CAST(:t AS jsonb),"
                " now() + make_interval(secs => :ttl), :u) RETURNING id"
            ),
            {
                "w": member.workspace_id,
                "p": project_id,
                "s": snapshot_id,
                "c": json.dumps(config.canonical()),
                "d": digest,
                "v": catalog.revision,
                "low": result.low_micro_usd,
                "high": result.high_micro_usd,
                "t": json.dumps(tokens),
                "ttl": VALIDITY_SECONDS,
                "u": member.user_id,
            },
        )
    ).scalar_one()
    await audit(db, member, "estimate.created", "estimate", estimate_id, {"price_version": catalog.revision})
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return await get_estimate(db, member, project_id, estimate_id)


async def get_estimate(
    db: AsyncSession, member: Member, project_id: UUID, estimate_id: UUID
) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                "SELECT e.id, e.project_id, e.snapshot_id, s.commit_sha, e.config, e.config_digest, e.price_version,"
                " e.low_micro_usd, e.high_micro_usd, e.token_estimate, e.created_at, e.expires_at,"
                " e.expires_at <= now() AS expired"
                " FROM estimates e JOIN snapshots s ON s.workspace_id = e.workspace_id AND s.id = e.snapshot_id"
                " WHERE e.workspace_id = :w AND e.project_id = :p AND e.id = :e"
            ),
            {"w": member.workspace_id, "p": project_id, "e": estimate_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Estimate not found.")
    tokens = row.token_estimate
    return {
        "id": row.id,
        "project_id": row.project_id,
        "snapshot_id": row.snapshot_id,
        "commit_sha": row.commit_sha,
        "config": row.config,
        "config_digest": row.config_digest,
        "price_version": row.price_version,
        "selected_files": tokens["files"]["selected"],
        "excluded_files": tokens["files"]["excluded"],
        "estimated_tokens": {"low": tokens["low"], "high": tokens["high"]},
        "cost_micro_usd": {"low": row.low_micro_usd, "high": row.high_micro_usd},
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "expired": row.expired,
        "disclaimer": DISCLAIMER,
    }
