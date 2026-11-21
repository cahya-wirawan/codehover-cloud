"""Verification stages: verify (deterministic citations) -> check (independent checker, one repair cycle).

verify needs no model: it extracts claims from every written page, validates each citation against the
immutable snapshot and records one `claim_checks` row per claim and citation.

check sends each page's deterministically valid claims, with only their exact cited lines, to the
checker role. When the checker cannot answer (exhausted retries, refusal, invalid answer after repair)
the claims stay `unchecked`. An answer cut off at the output limit is not repaired; its batch is checked
in halves, at most twice (`MAX_CHECK_SPLITS`). The checker is never replaced by the replay heuristic for a provider
preset, and its labels are never invented. One repair cycle may rewrite or remove unsupported and
partial claims; changed claims are validated and checked again, the original rows and page text are
kept (current = false / original_markdown_key), and the quality report counts every current claim.
"""

from __future__ import annotations

import functools
import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine import claims as claim_engine
from app.engine.keys import build_key
from app.jobs import calls
from app.jobs.generation import BuildContext, StageStop, _rows, bounded, context, gateway_for
from app.jobs.store import Apply, Claim


async def _sources(
    rt: Any, claimed: Claim, ctx: BuildContext, wanted: set[str]
) -> dict[str, claim_engine.SourceFile]:
    rows = await _rows(
        rt,
        claimed,
        "SELECT id, path, sha256, scan_status, blob_key FROM source_files WHERE workspace_id = :w AND snapshot_id = :s",
        s=ctx.snapshot_id,
    )
    files = {}
    for r in rows:
        lines = None
        if str(r.id) in wanted and r.blob_key and r.scan_status in claim_engine.TEXT_STATUSES:
            lines = tuple((await rt.store.get(r.blob_key)).decode("utf-8").split("\n"))
        files[str(r.id)] = claim_engine.SourceFile(str(r.id), r.path, r.sha256 or "", r.scan_status, lines)
    return files


async def _pages(rt: Any, claimed: Claim) -> list[tuple[Any, str]]:
    pages = await _rows(
        rt,
        claimed,
        "SELECT id, slug, markdown_key, original_markdown_key FROM guide_pages"
        " WHERE workspace_id = :w AND build_id = :b AND status = 'written' ORDER BY ordinal",
    )
    return [(p, (await rt.store.get(p.markdown_key)).decode("utf-8")) for p in pages]


def _rows_for(
    page_id: Any, claim: claim_engine.Claim, files: dict[str, claim_engine.SourceFile], revision: int
) -> list[dict[str, Any]]:
    rows = []
    for citation in claim.citations:
        problem = claim_engine.validate_citation(citation, files)
        source = files.get(citation.file_id or "")
        rows.append(
            {
                "page": page_id,
                "key": claim.key,
                "text": claim.text[:4000],
                "ordinal": claim.ordinal,
                "revision": revision,
                "citation": citation.raw[:200],
                "file": UUID(source.file_id) if source and problem != "file_not_in_snapshot" else None,
                "path": source.path if source else None,
                "start": citation.start_line,
                "end": max(citation.end_line, citation.start_line),
                "hash": source.sha256 if source and source.sha256 else None,
                "deterministic": "invalid" if problem else "valid",
                "status": "invalid" if problem else "unchecked",
                "reason": problem or "",
            }
        )
    return rows


async def _insert(db: AsyncSession, claimed: Claim, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        await db.execute(
            text(
                "INSERT INTO claim_checks(workspace_id, build_id, guide_page_id, claim_key, claim_text, ordinal, revision,"
                " citation, source_file_id, path, start_line, end_line, source_hash, deterministic, status, reason,"
                " evidence_lines, checker_model, checked_at)"
                " VALUES (:w, :b, :page, :key, :text, :ordinal, :revision, :citation, :file, :path, :start, :end, :hash,"
                " :deterministic, :status, :reason, CAST(:evidence AS jsonb), :model,"
                " CASE WHEN :status IN ('supported', 'partial', 'unsupported') THEN now() END)"
                " ON CONFLICT (guide_page_id, claim_key, citation) DO UPDATE SET status = EXCLUDED.status,"
                " reason = EXCLUDED.reason, evidence_lines = EXCLUDED.evidence_lines,"
                " checker_model = EXCLUDED.checker_model, checked_at = EXCLUDED.checked_at, current = true"
            ),
            {
                "w": claimed.workspace_id,
                "b": claimed.build,
                "evidence": json.dumps(row.get("evidence", [])),
                "model": row.get("model"),
                **{
                    k: row[k]
                    for k in (
                        "page",
                        "key",
                        "text",
                        "ordinal",
                        "revision",
                        "citation",
                        "file",
                        "path",
                        "start",
                        "end",
                        "hash",
                        "deterministic",
                        "status",
                        "reason",
                    )
                },
            },
        )


# ---------------------------------------------------------------- verify
async def verify(rt: Any, claimed: Claim) -> Apply | None:
    ctx = await context(rt, claimed)
    pages = await _pages(rt, claimed)
    extracted = [(page, claim_engine.extract(page.slug, markdown)) for page, markdown in pages]
    wanted = {c.file_id for _, cs in extracted for claim in cs for c in claim.citations if c.file_id}
    files = await _sources(rt, claimed, ctx, wanted)
    rows = [row for page, cs in extracted for claim in cs for row in _rows_for(page.id, claim, files, 0)]

    async def apply(db: AsyncSession) -> None:
        await _insert(db, claimed, rows)

    return apply


# ---------------------------------------------------------------- check
def _validate_checks(output: dict[str, Any], *, batch: list[claim_engine.Claim]) -> list[str]:
    return claim_engine.validate_checks(output, batch)


def _claim_status(rows: list[dict[str, Any]]) -> str:
    return "invalid" if any(r["deterministic"] == "invalid" for r in rows) else rows[0]["status"]


MAX_CHECK_SPLITS = 2  # a cut-off batch of 25 becomes at most 4 calls of 6-7 claims (7 calls in all)


async def _run_checker(
    rt: Any,
    claimed: Claim,
    ctx: BuildContext,
    page_claims: list[claim_engine.Claim],
    files: dict[str, claim_engine.SourceFile],
) -> dict[str, tuple[str, str, list[int], str | None]]:
    """claim key -> (status, reason, evidence lines, checker model). Missing answers are unchecked."""
    role = ctx.preset.checker
    model = ctx.model(role)
    gateway = await gateway_for(rt, claimed, model.provider)
    results: dict[str, tuple[str, str, list[int], str | None]] = {}
    valid = [
        c for c in page_claims if all(claim_engine.validate_citation(x, files) is None for x in c.citations)
    ]
    if gateway is None:
        raise StageStop("provider_unconfigured")

    async def run(prepared: Any, batch: list[claim_engine.Claim], splits: int) -> None:
        try:
            outcome = await calls.run(
                rt.sessions,
                gateway,
                claimed,
                prepared,
                functools.partial(_validate_checks, batch=batch),
                retry_delays=rt.retry_delays,
                repair_truncated=False,
            )
        except calls.BudgetExhausted as error:
            raise StageStop("budget_exhausted") from error
        except calls.BuildStopped as error:
            raise StageStop("cancelled") from error
        except calls.CallFailed as error:
            if error.code == calls.TRUNCATED and splits < MAX_CHECK_SPLITS and len(batch) > 1:
                # The answer for this many claims does not fit the checker's output limit: check halves.
                half = (len(batch) + 1) // 2
                for part in (batch[:half], batch[half:]):
                    await run(request_for(part), part, splits + 1)
                return
            for claim in batch:
                results[claim.key] = ("unchecked", f"checker unavailable: {error.code}", [], None)
            return
        for check in outcome.output["checks"]:
            results[check["claim_id"]] = (
                check["status"],
                check["reason"],
                check["evidence_line_numbers"],
                outcome.model_id,
            )
        for claim in batch:
            results.setdefault(claim.key, ("unchecked", "checker gave no answer", [], outcome.model_id))

    def request_for(batch: list[claim_engine.Claim]) -> Any:
        return claim_engine.check_request(
            workspace_id=str(claimed.workspace_id),
            preset=ctx.preset.name,
            model=model,
            role=role,
            prompt_version=ctx.prompt_version,
            batch=batch,
            files=files,
        )

    for prepared, batch in claim_engine.check_requests(
        workspace_id=str(claimed.workspace_id),
        preset=ctx.preset.name,
        model=model,
        role=role,
        prompt_version=ctx.prompt_version,
        claims=valid,
        files=files,
    ):
        await run(prepared, batch, 0)
    return results


async def check(rt: Any, claimed: Claim) -> Apply | None:
    ctx = await context(rt, claimed)
    pages = await _pages(rt, claimed)
    extracted = [(page, markdown, claim_engine.extract(page.slug, markdown)) for page, markdown in pages]
    wanted = {c.file_id for _, _, cs in extracted for claim in cs for c in claim.citations if c.file_id}
    files = await _sources(rt, claimed, ctx, wanted)

    async def one(page: Any, markdown: str, page_claims: list[claim_engine.Claim]) -> dict[str, Any]:
        results = await _run_checker(rt, claimed, ctx, page_claims, files)

        def rows_with_results(
            cs: list[claim_engine.Claim], revision: int, found: dict[str, Any]
        ) -> list[dict[str, Any]]:
            out = []
            for claim in cs:
                for row in _rows_for(page.id, claim, files, revision):
                    if row["deterministic"] == "valid" and claim.key in found:
                        status, reason, evidence, model = found[claim.key]
                        row.update(status=status, reason=reason[:1000], evidence=evidence, model=model)
                    out.append(row)
            return out

        original_rows = rows_with_results(page_claims, 0, results)
        flagged = [
            (claim, results[claim.key][0], results[claim.key][1])
            for claim in page_claims
            if claim.key in results and results[claim.key][0] in ("unsupported", "partial")
        ]
        outcome = {"page": page, "rows": original_rows, "repaired": None}
        if not flagged:
            return outcome
        writer = ctx.preset.writer
        model = ctx.model(writer)
        prepared = claim_engine.repair_request(
            workspace_id=str(claimed.workspace_id),
            preset=ctx.preset.name,
            model=model,
            role=writer,
            prompt_version=ctx.prompt_version,
            audience=ctx.config.variant,
            flagged=flagged,
            files=files,
        )
        flagged_claims = [f[0] for f in flagged]
        try:
            repaired = await calls.run(
                rt.sessions,
                await gateway_for(rt, claimed, model.provider),
                claimed,
                prepared,
                lambda o: claim_engine.validate_repair(o, flagged_claims),
                retry_delays=rt.retry_delays,
            )
        except calls.BudgetExhausted as error:
            raise StageStop("budget_exhausted") from error
        except calls.BuildStopped as error:
            raise StageStop("cancelled") from error
        except calls.CallFailed:
            return outcome  # no repair: the original claims and their labels stand
        new_markdown = claim_engine.apply_repair(markdown, page_claims, repaired.output)
        if new_markdown == markdown:
            return outcome
        new_claims = claim_engine.extract(page.slug, new_markdown)
        # A claim whose text and citations are unchanged keeps its checked result; only changed claims
        # are checked again (07 "rerun all changed checks").
        previous = {(c.text, tuple(x.raw for x in c.citations)): results.get(c.key) for c in page_claims}
        carried = {
            c.key: previous[(c.text, tuple(x.raw for x in c.citations))]
            for c in new_claims
            if previous.get((c.text, tuple(x.raw for x in c.citations))) is not None
        }
        rechecked = await _run_checker(
            rt, claimed, ctx, [c for c in new_claims if c.key not in carried], files
        )
        key = build_key(claimed.workspace_id, ctx.project_id, claimed.build, f"guide/{page.slug}.r1.md")
        await rt.store.put(key, new_markdown.encode(), "text/markdown; charset=utf-8")
        outcome["repaired"] = {"key": key, "rows": rows_with_results(new_claims, 1, {**carried, **rechecked})}
        return outcome

    outcomes = await bounded([functools.partial(one, page, markdown, cs) for page, markdown, cs in extracted])

    async def apply(db: AsyncSession) -> None:
        for outcome in outcomes:
            page = outcome["page"]
            await _insert(db, claimed, outcome["rows"])
            final_rows = outcome["rows"]
            if outcome["repaired"] is not None:
                await db.execute(
                    text(
                        "UPDATE claim_checks SET current = false WHERE workspace_id = :w AND guide_page_id = :p AND revision = 0"
                    ),
                    {"w": claimed.workspace_id, "p": page.id},
                )
                await _insert(db, claimed, outcome["repaired"]["rows"])
                final_rows = outcome["repaired"]["rows"]
                await db.execute(
                    text(
                        "UPDATE guide_pages SET original_markdown_key = markdown_key, markdown_key = :k"
                        " WHERE workspace_id = :w AND id = :p AND original_markdown_key IS NULL"
                    ),
                    {"k": outcome["repaired"]["key"], "w": claimed.workspace_id, "p": page.id},
                )
            by_claim: dict[str, list[dict[str, Any]]] = {}
            for row in final_rows:
                by_claim.setdefault(row["key"], []).append(row)
            statuses = [_claim_status(rs) for rs in by_claim.values()]
            await db.execute(
                text("UPDATE guide_pages SET check_status = :s WHERE workspace_id = :w AND id = :p"),
                {"s": claim_engine.page_status(statuses), "w": claimed.workspace_id, "p": page.id},
            )

    return apply
