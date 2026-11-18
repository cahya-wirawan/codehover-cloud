"""Quality report, waivers and the publication gate (06 "/builds/{bid}/quality", 07, 01).

A claim's status is `invalid` when any of its citations failed deterministic validation, otherwise the
checker's label (all rows of a claim share it). Coverage counts every current claim, including failed
and unchecked ones; uncited sentences are reported separately. The default gate blocks publication on
invalid citations, unchecked claims, unwaived unsupported claims and failed pages. Admins may waive
checked unsupported or partial claims with an audited reason; invalid and unchecked claims cannot be
waived, and waived claims stay visible.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.pagination import Position
from app.core.tenancy import set_context
from app.engine import claims as claim_engine
from app.modules.workspaces.access import Member, Reader
from app.modules.workspaces.service import audit

PER_CLAIM = (
    "SELECT c.guide_page_id, g.slug, c.claim_key, min(c.claim_text) AS claim_text, min(c.ordinal) AS ordinal,"
    " CASE WHEN bool_or(c.status = 'invalid') THEN 'invalid' ELSE min(c.status) END AS status,"
    " bool_and(c.waived_at IS NOT NULL) FILTER (WHERE c.status <> 'invalid') AS waived"
    " FROM claim_checks c JOIN guide_pages g ON g.workspace_id = c.workspace_id AND g.id = c.guide_page_id"
    " WHERE c.workspace_id = :w AND c.build_id = :b AND c.current"
    " GROUP BY c.guide_page_id, g.slug, c.claim_key"
)


async def _build(db: AsyncSession, member: Member, build_id: UUID) -> Any:
    row = (
        await db.execute(
            text("SELECT id, status FROM builds WHERE workspace_id = :w AND id = :b"),
            {"w": member.workspace_id, "b": build_id},
        )
    ).one_or_none()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Build not found.")
    if row.status != "ready":
        raise ApiError(409, "BUILD_NOT_READY", "This build is not ready yet.")
    return row


async def summary(db: AsyncSession, objects: ObjectStore, member: Member, build_id: UUID) -> dict[str, Any]:
    await _build(db, member, build_id)
    claims = list(await db.execute(text(PER_CLAIM), {"w": member.workspace_id, "b": build_id}))
    pages = list(
        await db.execute(
            text(
                "SELECT g.status, g.markdown_key, EXISTS (SELECT 1 FROM claim_checks c WHERE c.workspace_id = g.workspace_id"
                " AND c.guide_page_id = g.id AND c.current) AS has_claims FROM guide_pages g"
                " WHERE g.workspace_id = :w AND g.build_id = :b"
            ),
            {"w": member.workspace_id, "b": build_id},
        )
    )
    await db.commit()
    counts = {status: 0 for status in claim_engine.STATUSES}
    waived = {"unsupported": 0, "partial": 0}
    for claim in claims:
        counts[claim.status] += 1
        if claim.waived and claim.status in waived:
            waived[claim.status] += 1
    uncited = 0
    for page in pages:
        if page.markdown_key:
            uncited += claim_engine.uncited_sentences((await objects.get(page.markdown_key)).decode("utf-8"))
    failed_pages = sum(1 for p in pages if p.status == "failed")
    total = len(claims)
    uncited_pages = sum(1 for p in pages if p.status == "written" and not p.has_claims)
    blockers = claim_engine.publication_blockers(counts, waived["unsupported"], failed_pages, uncited_pages)
    return {
        "claims": total,
        "counts": counts,
        "waived": waived,
        "supported_fraction": round(counts["supported"] / total, 4) if total else None,
        "uncited_sentences": uncited,
        "pages": {"total": len(pages), "failed": failed_pages},
        "publishable": not blockers,
        "blockers": blockers,
        "requires_acknowledgment": counts["partial"] > 0,
    }


async def checks(
    db: AsyncSession, member: Member, build_id: UUID, status: str | None, after: Position | None, limit: int
) -> list[dict[str, Any]]:
    await _build(db, member, build_id)
    rows = await db.execute(
        text(
            "SELECT c.id, c.created_at, g.slug, c.claim_key, c.claim_text, c.citation, c.path, c.start_line,"
            " c.end_line, c.deterministic, c.status, c.reason, c.evidence_lines, c.checker_model,"
            " c.waived_at IS NOT NULL AS waived, c.waived_reason"
            " FROM claim_checks c JOIN guide_pages g ON g.workspace_id = c.workspace_id AND g.id = c.guide_page_id"
            " WHERE c.workspace_id = :w AND c.build_id = :b AND c.current"
            " AND (CAST(:status AS text) IS NULL OR c.status = :status)"
            " AND (CAST(:t AS timestamptz) IS NULL OR (c.created_at, c.id) > (CAST(:t AS timestamptz), CAST(:i AS uuid)))"
            " ORDER BY c.created_at, c.id LIMIT :n"
        ),
        {
            "w": member.workspace_id,
            "b": build_id,
            "status": status,
            "t": after.created_at if after else None,
            "i": after.id if after else None,
            "n": limit,
        },
    )
    result = [dict(r._mapping) for r in rows]
    await db.commit()
    return result


async def waive(
    db: AsyncSession, member: Member, build_id: UUID, claim_keys: list[str], reason: str
) -> dict[str, Any]:
    await _build(db, member, build_id)
    found = {
        r.claim_key: r
        for r in await db.execute(
            text(PER_CLAIM + " HAVING c.claim_key = ANY(:keys)"),
            {"w": member.workspace_id, "b": build_id, "keys": claim_keys},
        )
    }
    missing = sorted(set(claim_keys) - set(found))
    if missing:
        raise ApiError(404, "NOT_FOUND", "Some claims were not found.", details={"claim_keys": missing[:20]})
    refused = sorted(k for k, r in found.items() if r.status not in ("unsupported", "partial"))
    if refused:
        raise ApiError(
            409,
            "WAIVER_NOT_ALLOWED",
            "Only checked unsupported or partial claims can be waived; invalid citations and unchecked claims cannot.",
            details={"claim_keys": refused[:20]},
        )
    await db.execute(
        text(
            "UPDATE claim_checks SET waived_by = :u, waived_reason = :r, waived_at = now()"
            " WHERE workspace_id = :w AND build_id = :b AND current AND claim_key = ANY(:keys)"
            " AND status IN ('unsupported', 'partial') AND waived_at IS NULL"
        ),
        {"u": member.user_id, "r": reason, "w": member.workspace_id, "b": build_id, "keys": claim_keys},
    )
    await audit(
        db, member, "claims.waived", "build", build_id, {"claim_keys": sorted(claim_keys), "reason": reason}
    )
    await db.commit()
    await set_context(db, workspace_id=member.workspace_id, user_id=member.user_id)
    return {"waived": sorted(claim_keys)}


async def page_claims(db: AsyncSession, member: Reader, page_id: UUID) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            "SELECT claim_key, min(claim_text) AS text, min(ordinal) AS ordinal,"
            " CASE WHEN bool_or(status = 'invalid') THEN 'invalid' ELSE min(status) END AS status,"
            " min(reason) FILTER (WHERE status <> 'unchecked' OR reason <> '') AS reason,"
            " bool_and(waived_at IS NOT NULL) FILTER (WHERE status <> 'invalid') AS waived,"
            " json_agg(json_build_object('citation', citation, 'path', path, 'start_line', start_line,"
            "   'end_line', end_line, 'deterministic', deterministic) ORDER BY citation) AS citations"
            " FROM claim_checks WHERE workspace_id = :w AND guide_page_id = :p AND current"
            " GROUP BY claim_key ORDER BY min(ordinal)"
        ),
        {"w": member.workspace_id, "p": page_id},
    )
    return [{**dict(r._mapping), "waived": bool(r.waived)} for r in rows]
