"""Snapshots before builds and preflight estimates (06 "/projects/{pid}/snapshots", "/estimates"; 01 FR-007).

A snapshot is resolved to an exact commit and acquired by a worker task; the estimate is computed from
its scan and the approved catalog, stored immutably with its config digest and price revision, and
expires after 30 minutes. Starting a generation build later requires that exact estimate (A05).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.github.app import GitHubApp, GitHubError
from app.adapters.github.public import PublicGitHub
from app.core.errors import ApiError
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.engine import ENGINE_VERSION, GENERATION_PROMPT_VERSION
from app.engine.catalog import Catalog
from app.engine.estimate import VALIDITY_SECONDS, GenerationConfig, ScannedFile, config_digest, estimate
from app.engine.globs import GlobBudgetExceeded
from app.jobs import store as jobs
from app.modules.providers import keys as provider_keys
from app.modules.usage import ledger
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit

DISCLAIMER = "Illustrative estimate; actual usage is metered and never exceeds the build cap."


async def _source(db: AsyncSession, member: Member, project_id: UUID) -> Any:
    source = (
        await db.execute(
            text(
                "SELECT p.tracked_ref, r.provider_repo_id, r.access_revoked_at, c.installation_id, c.status,"
                " c.provider"
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
    db: AsyncSession,
    github: GitHubApp | None,
    member: Member,
    project_id: UUID,
    ref: str | None,
    *,
    public: PublicGitHub | None = None,
) -> tuple[str, str]:
    """(requested ref, exact commit SHA). Commits first: no transaction is open during the GitHub call."""
    source = await _source(db, member, project_id)
    await db.commit()
    if source.provider == "upload":
        raise ApiError(
            409, "NOT_A_GITHUB_PROJECT", "This project is an uploaded archive; upload a new version."
        )
    if source.status != "active" or source.access_revoked_at is not None:
        raise ApiError(409, "CONNECTION_INACTIVE", "Reconnect GitHub to build this project.")
    if source.provider == "public":  # no App: the repository must still be public (08)
        if public is None:
            raise ApiError(
                503, "UNAVAILABLE", "Public GitHub repositories are not available.", retryable=True
            )
        try:
            sha = await public.resolve_ref(source.provider_repo_id, ref or source.tracked_ref)
        except GitHubError as error:
            if error.status in (404, 422) and await _still_public(public, source):
                raise ApiError(422, "REF_NOT_FOUND", "That branch, tag or commit does not exist.") from error
            raise public_unavailable(error) from error
        await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
        return ref or source.tracked_ref, sha
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


async def _still_public(public: PublicGitHub, source: Any) -> bool:
    """Tells a missing ref apart from a repository that is no longer public."""
    try:
        await public.repository(source.provider_repo_id)
    except GitHubError:
        return False
    return True


def public_unavailable(error: GitHubError) -> ApiError:
    """A public-repository call failed: gone or private (never told apart), rate limited, or down."""
    if error.reason == "not_found":
        return ApiError(404, "REPOSITORY_NOT_PUBLIC", "GitHub has no public repository with that name.")
    if error.reason == "rate_limited":
        return ApiError(
            503, "GITHUB_RATE_LIMITED", "GitHub's rate limit was reached; try again later.", retryable=True
        )
    return ApiError(503, "UNAVAILABLE", "GitHub is not reachable right now.", retryable=True)


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
    db: AsyncSession,
    github: GitHubApp | None,
    settings: Settings,
    member: Member,
    project_id: UUID,
    ref: str | None,
    *,
    public: PublicGitHub | None = None,
) -> dict[str, Any]:
    requested, sha = await resolve_commit(db, github, member, project_id, ref, public=public)
    snapshot = await upsert_snapshot(db, member, project_id, sha, requested)
    if snapshot.status in ("pending", "acquiring", "failed"):  # rejected and scanned are final
        await _admit_acquisition(db, settings, member, snapshot.id)
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


async def _admit_acquisition(db: AsyncSession, settings: Settings, member: Member, snapshot_id: UUID) -> None:
    """Acquisitions download and scan archives, so a workspace may run only a few at once. The usage
    account row lock serializes this check with build admission."""
    await ledger.current_account(db, member.workspace_id, settings.generation_allowance_micro_usd, lock=True)
    live = (
        await db.execute(
            text(
                "SELECT count(*) FROM build_tasks WHERE workspace_id = :w AND snapshot_id IS NOT NULL"
                " AND snapshot_id <> :s AND state IN ('pending', 'leased', 'retry_wait')"
            ),
            {"w": member.workspace_id, "s": snapshot_id},
        )
    ).scalar_one()
    if live >= settings.max_active_acquisitions_per_workspace:
        await db.rollback()
        raise ApiError(
            409,
            "ACQUISITION_LIMIT",
            "This workspace is already acquiring the maximum number of snapshots.",
            details={"limit": settings.max_active_acquisitions_per_workspace},
            retryable=True,
        )


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


async def _variant_config(
    db: AsyncSession, member: Member, project_id: UUID, snapshot_id: UUID, config: GenerationConfig
) -> GenerationConfig:
    """A variant (FR-026) of a ready generated build of the same project and snapshot. Its scope is the
    base's, so it explains the same files and plans the same pages; only language, level and preset
    are the caller's."""
    base = (
        await db.execute(
            text(
                "SELECT b.snapshot_id, b.status, e.config FROM builds b"
                " JOIN estimates e ON e.workspace_id = b.workspace_id AND e.id = b.estimate_id"
                " WHERE b.workspace_id = :w AND b.project_id = :p AND b.id = CAST(:b AS uuid)"
            ),
            {"w": member.workspace_id, "p": project_id, "b": config.variant_of},
        )
    ).one_or_none()
    if base is None:
        raise ApiError(404, "NOT_FOUND", "Base build not found in this project, or it was not generated.")
    if base.status != "ready":
        raise ApiError(409, "BUILD_NOT_READY", "A variant needs a ready base build.")
    if base.snapshot_id != snapshot_id:
        raise ApiError(422, "VARIANT_SNAPSHOT_MISMATCH", "A variant uses its base build's snapshot.")
    original = GenerationConfig.model_validate(base.config)
    if original.variant_of is not None:
        raise ApiError(
            422, "VARIANT_OF_VARIANT", "Make variants of the original build, not of another variant."
        )
    if original.variant == config.variant:
        raise ApiError(
            422, "VARIANT_UNCHANGED", "Choose another language or reader level than the base build's."
        )
    if config.translate and (
        original.levels != config.levels or original.languages[0] == config.languages[0]
    ):
        raise ApiError(
            422,
            "TRANSLATION_NEEDS_LANGUAGE",
            "A translation changes only the language: keep the base build's reader level.",
        )
    return config.model_copy(
        update={
            "include": original.include,
            "exclude": original.exclude,
            "max_files": original.max_files,
            "guide_pages": original.guide_pages,
        }
    )


def preset_providers(catalog: Catalog, name: str) -> set[str]:
    preset = catalog.preset(name)
    if preset is None:
        return set()
    return {catalog.model(preset.writer.model).provider, catalog.model(preset.checker.model).provider}


def preset_available(
    catalog: Catalog, name: str, settings: Settings, own_keys: set[str] | None = None
) -> bool:
    """Every provider of the preset is configured by the operator, or (M6) the workspace has its own key."""
    providers = preset_providers(catalog, name)
    return bool(providers) and providers <= settings.configured_providers() | (own_keys or set())


def own_key_usable(catalog: Catalog, name: str, own_keys: set[str]) -> bool:
    """An own-key build needs a key for every paid provider of the preset (replay needs none)."""
    paid = preset_providers(catalog, name) - {"replay"}
    return bool(paid) and paid <= own_keys


def batch_available(catalog: Catalog, name: str) -> bool:
    preset = catalog.preset(name)
    return preset is not None and catalog.model(preset.writer.model).batch_price is not None


async def create_estimate(
    db: AsyncSession,
    catalog: Catalog,
    settings: Settings,
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
    if config.variant_of is not None:
        config = await _variant_config(db, member, project_id, snapshot_id, config)
    preset = catalog.preset(config.model_preset)
    if preset is None:
        raise ApiError(422, "PRESET_UNKNOWN", "This model preset is not in the approved catalog.")
    own = await provider_keys.active_providers(db, member.workspace_id)
    if not preset_available(catalog, config.model_preset, settings, own):
        raise ApiError(422, "PRESET_UNAVAILABLE", "This model preset's provider is not configured.")
    if config.batch and not batch_available(catalog, config.model_preset):
        raise ApiError(
            422,
            "BATCH_UNAVAILABLE",
            "This preset's writer model has no batch price; estimate without batching.",
        )
    try:
        files = await scanned_files(db, member.workspace_id, snapshot_id)
        # Bounded but CPU-bound: keep the event loop serving other requests meanwhile.
        result = await asyncio.to_thread(estimate, files, config, catalog, preset)
    except GlobBudgetExceeded as error:
        raise ApiError(422, "SCOPE_TOO_COMPLEX", "Simplify the include and exclude patterns.") from error
    if not result.selected:
        raise ApiError(422, "SCOPE_EMPTY", "No supported source files match this scope.")
    digest = config_digest(
        config,
        commit_sha=snapshot["commit_sha"],
        revision=catalog.revision,
        engine=ENGINE_VERSION,
        prompts=GENERATION_PROMPT_VERSION,
    )
    # A family (several languages, FR-026) costs one build per language: the base plans the guide and each
    # variant copies the plan, so the per-language estimate bounds each of them. A translated variant (P07)
    # costs the translation estimate instead.
    languages = len(config.family) if config.variant_of is None else 1
    per_language = {"low": result.low_micro_usd, "high": result.high_micro_usd}
    per_translation = {"low": result.translated_low_micro_usd, "high": result.translated_high_micro_usd}
    if config.variant_of is not None:
        total = per_translation if config.translate else per_language
    else:
        each = per_translation if config.translate else per_language
        total = {k: per_language[k] + each[k] * (languages - 1) for k in ("low", "high")}
    tokens = {
        **result.tokens,
        "files": {"selected": len(result.selected), "excluded": result.excluded},
        "per_language": per_language,
        "languages": languages,
    }
    if config.translate:
        tokens["per_translation"] = per_translation
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
                "low": total["low"],
                "high": total["high"],
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


async def variant_estimate(
    db: AsyncSession,
    member: Member,
    project_id: UUID,
    approved: Any,
    config: GenerationConfig,
    catalog: Catalog,
) -> tuple[UUID, str]:
    """The estimate row of one variant build of a several-language estimate (FR-026): the variant's config,
    the family's per-language cost and validity, so the variant retries and reads like any variant."""
    family = (
        await db.execute(
            text("SELECT token_estimate, expires_at FROM estimates WHERE workspace_id = :w AND id = :e"),
            {"w": member.workspace_id, "e": approved.id},
        )
    ).one()
    per_language = family.token_estimate.get("per_language") or {"low": 0, "high": approved.high_micro_usd}
    if config.translate and family.token_estimate.get("per_translation"):
        per_language = family.token_estimate["per_translation"]
    tokens = {**family.token_estimate, "languages": 1}
    digest = config_digest(
        config,
        commit_sha=approved.commit_sha,
        revision=catalog.revision,
        engine=ENGINE_VERSION,
        prompts=GENERATION_PROMPT_VERSION,
    )
    estimate_id = (
        await db.execute(
            text(
                "INSERT INTO estimates(workspace_id, project_id, snapshot_id, config, config_digest, price_version,"
                " low_micro_usd, high_micro_usd, token_estimate, expires_at, created_by)"
                " VALUES (:w, :p, :s, CAST(:c AS jsonb), :d, :v, :low, :high, CAST(:t AS jsonb), :x, :u) RETURNING id"
            ),
            {
                "w": member.workspace_id,
                "p": project_id,
                "s": approved.snapshot_id,
                "c": json.dumps(config.canonical()),
                "d": digest,
                "v": catalog.revision,
                "low": per_language["low"],
                "high": per_language["high"],
                "t": json.dumps(tokens),
                "x": family.expires_at,
                "u": member.user_id,
            },
        )
    ).scalar_one()
    return estimate_id, digest


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
        "per_language_micro_usd": tokens.get(
            "per_language", {"low": row.low_micro_usd, "high": row.high_micro_usd}
        ),
        "per_translation_micro_usd": tokens.get("per_translation"),
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "expired": row.expired,
        "disclaimer": DISCLAIMER,
    }
