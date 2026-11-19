"""Generation stages of estimate-based builds: explain -> overview -> plan -> guide (07, 19).

Each handler loads its evidence and commits, runs its provider calls through `calls.run` (budgeted,
fenced, cached) with at most four calls in flight per build, then returns an `apply` callback that the
fenced `store.complete` runs. A call that fails permanently or stays invalid after its repair does not
fail the build: that unit falls back to the deterministic source-only answer and is labeled, so the
draft stays reviewable (07 "Partial generation"). Running out of budget or a cancelled build stops the
stage (StageError budget_exhausted / cancelled).
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.models.replay import ReplayGateway
from app.engine import generate
from app.engine.catalog import Catalog, Model, Preset, Role
from app.engine.estimate import GenerationConfig, ScannedFile, select_files
from app.engine.globs import match as glob_match
from app.engine.keys import build_key
from app.jobs import batching, calls
from app.jobs.store import Apply, Claim

MAX_CONCURRENT_CALLS = 4  # per build (07)
_REPLAY = ReplayGateway()


class StageStop(Exception):
    """Raised to the stage runner as a StageError code (budget_exhausted, cancelled, or a lost lease)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BuildContext:
    project_id: UUID
    snapshot_id: UUID
    commit_sha: str
    config: GenerationConfig
    preset: Preset
    catalog: Catalog
    prompt_version: str

    def model(self, role: Role) -> Model:
        return self.catalog.model(role.model)


async def _rows(rt: Any, claimed: Claim, sql: str, **params: Any) -> list[Any]:
    from app.core.tenancy import set_context

    async with rt.sessions() as db:
        await set_context(db, workspace_id=claimed.workspace_id)
        rows = list(await db.execute(text(sql), {"w": claimed.workspace_id, "b": claimed.build, **params}))
        await db.commit()
    return rows


async def context(rt: Any, claimed: Claim) -> BuildContext:
    row = (
        await _rows(
            rt,
            claimed,
            "SELECT b.project_id, b.snapshot_id, b.config, b.prompt_version, s.commit_sha FROM builds b"
            " JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
            " WHERE b.workspace_id = :w AND b.id = :b",
        )
    )[0]
    config = GenerationConfig.model_validate(row.config["generation"])
    catalog: Catalog = rt.catalog
    preset = catalog.preset(config.model_preset)
    if catalog.revision != row.config["catalog"] or preset is None:
        raise StageStop("catalog_changed")  # never price a build with a revision it was not approved under
    return BuildContext(
        row.project_id, row.snapshot_id, row.commit_sha, config, preset, catalog, row.prompt_version
    )


async def load_files(rt: Any, claimed: Claim, ctx: BuildContext) -> list[generate.FileInfo]:
    scanned = await _rows(
        rt,
        claimed,
        "SELECT id, path, scan_status, byte_count, COALESCE(line_count, 0) AS lines, language, sha256, blob_key"
        " FROM source_files WHERE workspace_id = :w AND snapshot_id = :s ORDER BY path",
        s=ctx.snapshot_id,
    )
    selected, _ = select_files(
        [ScannedFile(str(r.id), r.path, r.scan_status, r.byte_count, r.lines) for r in scanned], ctx.config
    )
    by_id = {str(r.id): r for r in scanned}
    symbols = await _rows(
        rt,
        claimed,
        "SELECT source_file_id, symbol_key, name, kind, start_line, end_line, signature, parent_key, rank, card"
        " FROM symbols WHERE workspace_id = :w AND build_id = :b ORDER BY start_line, symbol_key",
    )
    per_file: dict[str, list[generate.SymbolInfo]] = {}
    rank: dict[str, float] = {}
    for s in symbols:
        source = (s.card or {}).get("source", {})
        per_file.setdefault(str(s.source_file_id), []).append(
            generate.SymbolInfo(
                s.symbol_key,
                s.name,
                s.kind,
                s.start_line,
                s.end_line,
                s.signature or "",
                tuple(source.get("params", [])),
                source.get("docstring"),
                s.parent_key,
            )
        )
        rank[str(s.source_file_id)] = max(rank.get(str(s.source_file_id), 0.0), s.rank)
    files = []
    for f in selected:
        r = by_id[f.id]
        text_bytes = await rt.store.get(r.blob_key)
        files.append(
            generate.FileInfo(
                f.id,
                r.path,
                r.language or "",
                r.sha256 or "",
                tuple(text_bytes.decode("utf-8").split("\n")),
                tuple(per_file.get(f.id, [])),
            )
        )
    files.sort(key=lambda f: (-rank.get(f.file_id, 0.0), f.path))  # rank orders work inside the scope
    return files


async def bounded[T](items: list[Callable[[], Awaitable[T]]]) -> list[T]:
    """Run at most MAX_CONCURRENT_CALLS units at once; the first stop (budget, cancel, lease) wins."""
    gate = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

    async def one(item: Callable[[], Awaitable[T]]) -> T:
        async with gate:
            return await item()

    tasks = [asyncio.create_task(one(item)) for item in items]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def gateway_for(rt: Any, claimed: Claim, provider: str) -> Any:
    """The gateway that serves this build: the operator's, or for an own-key build (M6, T24) one made with
    the workspace's live key, checked on every call so a removed key stops the build."""
    funding = await _rows(
        rt, claimed, "SELECT config->>'funding' AS funding FROM builds WHERE workspace_id = :w AND id = :b"
    )
    if not funding or funding[0].funding != "own_key" or provider == "replay":
        gateway = rt.gateways.get(provider)
        if gateway is None:  # the worker has no credentials for this provider
            raise StageStop("provider_unconfigured")
        return gateway
    from app.modules.providers import keys

    live = await _rows(
        rt,
        claimed,
        "SELECT id FROM provider_keys WHERE workspace_id = :w AND provider = :p AND revoked_at IS NULL",
        p=provider,
    )
    if not live or rt.sealer is None or rt.own_key_gateway is None:
        raise StageStop("provider_key_missing")
    cache_key = (claimed.workspace_id, live[0].id)
    if cache_key not in rt.own_key_cache:
        from app.core.tenancy import set_context

        async with rt.sessions() as db:
            await set_context(db, workspace_id=claimed.workspace_id)
            key = await keys.open_key(db, rt.sealer, claimed.workspace_id, provider)
            await db.commit()
        if key is None:
            raise StageStop("provider_key_missing")
        rt.own_key_cache[cache_key] = rt.own_key_gateway(key)
    return rt.own_key_cache[cache_key]


async def answer(
    rt: Any, claimed: Claim, prepared: generate.Prepared, validate: Callable[[dict[str, Any]], list[str]]
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """(output, status, provenance). Falls back to the deterministic replay answer when the provider
    cannot produce a valid one; budget, cancellation and lease loss stop the stage instead."""
    gateway = await gateway_for(rt, claimed, prepared.request.model.provider)
    try:
        outcome = await calls.run(
            rt.sessions, gateway, claimed, prepared, validate, retry_delays=rt.retry_delays
        )
    except calls.BudgetExhausted as error:
        raise StageStop("budget_exhausted") from error
    except calls.BuildStopped as error:
        raise StageStop("cancelled") from error
    except calls.CallFailed as error:
        if error.kind == "transient":  # retries exhausted: let the task retry later (checkpoints kept)
            raise StageStop("provider_unavailable") from error
        fallback = await _REPLAY.generate(prepared.request)
        return fallback.output, "failed", {"generator": "replay-fallback", "error": error.code}
    provenance = {
        "generator": "model",
        "model": outcome.model_id,
        "prompt_version": prepared.request.prompt_version,
        "cached": outcome.cached,
    }
    return outcome.output, "generated", provenance


def _role_request(ctx: BuildContext, role: Role) -> dict[str, Any]:
    return {
        "preset": ctx.preset.name,
        "model": ctx.model(role),
        "role": role,
        "prompt_version": ctx.prompt_version,
    }


def _writer_request(ctx: BuildContext) -> dict[str, Any]:
    """The writer writes for the build's language and reader level (FR-026); the checker does not write."""
    return {**_role_request(ctx, ctx.preset.writer), "audience": ctx.config.variant}


# ---------------------------------------------------------------- explain
async def explain(rt: Any, claimed: Claim) -> Apply | None:
    ctx = await context(rt, claimed)
    files = await load_files(rt, claimed, ctx)
    map_entries = generate.repo_map(files)
    related_rows = await _rows(
        rt,
        claimed,
        "SELECT DISTINCT e.source_file_id, o.source_file_id AS target_file_id, o.name, o.kind, o.signature, f.path,"
        " e.resolution"
        " FROM reference_edges e"
        " JOIN symbols o ON o.workspace_id = e.workspace_id AND o.id = e.to_symbol_id"
        " JOIN source_files f ON f.workspace_id = o.workspace_id AND f.id = o.source_file_id"
        " WHERE e.workspace_id = :w AND e.build_id = :b AND o.source_file_id <> e.source_file_id",
    )
    related: dict[str, list[dict[str, Any]]] = {}
    in_scope = {f.file_id for f in files}
    for r in related_rows:
        if str(r.source_file_id) not in in_scope or str(r.target_file_id) not in in_scope:
            continue  # files outside the approved scope never reach a provider prompt (07)
        items = related.setdefault(str(r.source_file_id), [])
        if len(items) < 20:
            items.append(
                {
                    "name": r.name,
                    "kind": r.kind,
                    "path": r.path,
                    "signature": r.signature or "",
                    "resolution": r.resolution,
                }
            )
    work: list[Callable[[], Awaitable[Any]]] = []
    batchable: list[tuple[generate.Prepared, Callable[[dict[str, Any]], list[str]]]] = []
    for f in files:
        for chunk in generate.chunks(f):
            prepared = generate.explain_request(
                workspace_id=str(claimed.workspace_id),
                file=f,
                chunk=chunk,
                related=related.get(f.file_id, []),
                map_entries=map_entries,
                **_writer_request(ctx),
            )

            async def unit(
                f: generate.FileInfo = f,
                chunk: generate.Chunk = chunk,
                prepared: generate.Prepared = prepared,
            ) -> Any:
                output, status, provenance = await answer(
                    rt, claimed, prepared, lambda o: generate.validate_explanation(o, chunk)
                )
                return f, chunk, output, status, provenance

            work.append(functools.partial(unit, f, chunk, prepared))
            batchable.append((prepared, functools.partial(generate.validate_explanation, chunk=chunk)))
    if ctx.config.batch and batchable:  # M6, T24: one provider batch fills the cache first
        gateway = await gateway_for(rt, claimed, ctx.model(ctx.preset.writer).provider)
        try:
            await batching.prefill(rt.sessions, gateway, claimed, batchable)
        except calls.BudgetExhausted as error:
            raise StageStop("budget_exhausted") from error
        except calls.BuildStopped as error:
            raise StageStop("cancelled") from error
    results = await bounded(work)

    cards: dict[tuple[str, str], dict[str, Any]] = {}
    per_file: dict[str, dict[str, Any]] = {}
    for f, chunk, output, status, provenance in results:
        entry = per_file.setdefault(f.file_id, {"chunks": [], "statuses": set()})
        entry["statuses"].add(status)
        entry["chunks"].append(
            {
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "incomplete": chunk.incomplete,
                "summary": output["summary"],
                "notes": output["notes"],
                "status": status,
                "provenance": provenance,
            }
        )
        for card in output["symbols"]:
            cards[(f.file_id, card["symbol_key"])] = {**card, "status": status, "provenance": provenance}

    async def apply(db: AsyncSession) -> None:
        for (file_id, key), card in cards.items():
            await db.execute(
                text(
                    "UPDATE symbols SET card = jsonb_build_object('source', card->'source') || CAST(:c AS jsonb)"
                    " WHERE workspace_id = :w AND build_id = :b AND source_file_id = :f AND symbol_key = :k"
                ),
                {
                    "c": json.dumps(card),
                    "w": claimed.workspace_id,
                    "b": claimed.build,
                    "f": UUID(file_id),
                    "k": key,
                },
            )
        for file_id, entry in per_file.items():
            ordered = sorted(entry["chunks"], key=lambda c: c["start_line"])
            statuses = entry["statuses"]
            status = (
                "generated"
                if statuses == {"generated"}
                else ("failed" if statuses == {"failed"} else "partial")
            )
            await db.execute(
                text(
                    "INSERT INTO file_explanations(workspace_id, build_id, source_file_id, status, summary, chunks, provenance)"
                    " VALUES (:w, :b, :f, :s, :summary, CAST(:chunks AS jsonb), CAST(:p AS jsonb))"
                    " ON CONFLICT (build_id, source_file_id) DO UPDATE SET status = EXCLUDED.status,"
                    " summary = EXCLUDED.summary, chunks = EXCLUDED.chunks, provenance = EXCLUDED.provenance"
                ),
                {
                    "w": claimed.workspace_id,
                    "b": claimed.build,
                    "f": UUID(file_id),
                    "s": status,
                    # The file card shows the first chunk's summary followed by later chunk summaries (07).
                    "summary": "\n\n".join(c["summary"] for c in ordered)[:20000],
                    "chunks": json.dumps(ordered),
                    "p": json.dumps({"preset": ctx.preset.name, "prompt_version": ctx.prompt_version}),
                },
            )

    return apply


# ---------------------------------------------------------------- overview and plan
async def _summaries(rt: Any, claimed: Claim, files: list[generate.FileInfo]) -> list[dict[str, Any]]:
    rows = await _rows(
        rt,
        claimed,
        "SELECT source_file_id, summary FROM file_explanations WHERE workspace_id = :w AND build_id = :b",
    )
    by_file = {str(r.source_file_id): r.summary for r in rows}
    return [
        {
            "file_id": f.file_id,
            "path": f.path,
            "lines": len(f.lines),
            "summary": by_file.get(f.file_id, "")[: generate.SUMMARY_MAX_CHARS],
        }
        for f in files
    ]


async def _readme(rt: Any, claimed: Claim, ctx: BuildContext) -> str:
    """A root README that is in scope and passed the scan (excluded files never reach prompts, 07)."""
    rows = await _rows(
        rt,
        claimed,
        "SELECT path, blob_key FROM source_files WHERE workspace_id = :w AND snapshot_id = :s"
        " AND scan_status IN ('selected', 'unsupported') AND blob_key IS NOT NULL AND path ~* '^readme(\\.[a-z0-9]+)?$'"
        " ORDER BY path LIMIT 1",
        s=ctx.snapshot_id,
    )
    if not rows:
        return ""
    path = rows[0].path
    in_scope = any(glob_match(path, g) for g in ctx.config.include) and not any(
        glob_match(path, g) for g in ctx.config.exclude
    )
    if not in_scope:
        return ""
    data: bytes = await rt.store.get(rows[0].blob_key)
    return data.decode("utf-8", "replace")[: generate.README_MAX_CHARS]


async def _store_json(
    rt: Any, claimed: Claim, ctx: BuildContext, name: str, value: dict[str, Any]
) -> tuple[str, str, int]:
    body = json.dumps(value, sort_keys=True).encode()
    key = build_key(claimed.workspace_id, ctx.project_id, claimed.build, name)
    sha = await rt.store.put(key, body, "application/json")
    return key, sha, len(body)


def _artifact(
    claimed: Claim, kind: str, stored: tuple[str, str, int], content_type: str = "application/json"
) -> Apply:
    key, sha, size = stored

    async def apply(db: AsyncSession) -> None:
        await db.execute(
            text(
                "INSERT INTO artifacts(workspace_id, build_id, kind, object_key, sha256, byte_count, content_type)"
                " VALUES (:w, :b, :k, :key, :sha, :n, :ct) ON CONFLICT (build_id, kind, variant)"
                " DO UPDATE SET object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256, byte_count = EXCLUDED.byte_count"
            ),
            {
                "w": claimed.workspace_id,
                "b": claimed.build,
                "k": kind,
                "key": key,
                "sha": sha,
                "n": size,
                "ct": content_type,
            },
        )

    return apply


async def overview(rt: Any, claimed: Claim) -> Apply | None:
    ctx = await context(rt, claimed)
    files = await load_files(rt, claimed, ctx)
    summaries = await _summaries(rt, claimed, files)
    prepared = generate.overview_request(
        workspace_id=str(claimed.workspace_id),
        summaries=summaries,
        map_entries=generate.repo_map(files),
        readme=await _readme(rt, claimed, ctx),
        **_writer_request(ctx),
    )
    ids = {f.file_id for f in files}
    output, status, provenance = await answer(
        rt, claimed, prepared, lambda o: generate.validate_overview(o, ids)
    )
    stored = await _store_json(
        rt, claimed, ctx, "overview.json", {**output, "status": status, "provenance": provenance}
    )
    return _artifact(claimed, "overview", stored)


async def _base_plan(rt: Any, claimed: Claim, ctx: BuildContext) -> Apply:
    """A variant reuses its base build's guide plan: the same slugs, order, purposes and source ranges,
    so page anchors are identical across variants (FR-026). Titles are rewritten with each page."""
    pages = await _rows(
        rt,
        claimed,
        "SELECT slug, title, ordinal, purpose, source_ranges FROM guide_pages"
        " WHERE workspace_id = :w AND build_id = :base ORDER BY ordinal",
        base=ctx.config.variant_of,
    )
    output = {
        "pages": [
            {"slug": p.slug, "title": p.title, "purpose": p.purpose, "source_ranges": p.source_ranges}
            for p in pages
        ],
        "variant_of": ctx.config.variant_of,
    }

    async def apply(db: AsyncSession) -> None:
        for page in pages:
            await db.execute(
                text(
                    "INSERT INTO guide_pages(workspace_id, build_id, slug, title, ordinal, purpose, source_ranges)"
                    " VALUES (:w, :b, :slug, :title, :o, :purpose, CAST(:ranges AS jsonb))"
                    " ON CONFLICT (build_id, slug) DO NOTHING"
                ),
                {
                    "w": claimed.workspace_id,
                    "b": claimed.build,
                    "slug": page.slug,
                    "title": page.title,
                    "o": page.ordinal,
                    "purpose": page.purpose,
                    "ranges": json.dumps(page.source_ranges),
                },
            )
        await _artifact(
            claimed,
            "guide_plan",
            await _store_json(
                rt,
                claimed,
                ctx,
                "guide-plan.json",
                {**output, "status": "copied", "provenance": {"generator": "variant"}},
            ),
        )(db)

    return apply


async def plan(rt: Any, claimed: Claim) -> Apply | None:
    ctx = await context(rt, claimed)
    if ctx.config.variant_of is not None:
        return await _base_plan(rt, claimed, ctx)
    files = await load_files(rt, claimed, ctx)
    summaries = await _summaries(rt, claimed, files)
    bounds = generate.page_bounds(len(files), ctx.config.guide_pages)
    prepared = generate.plan_request(
        workspace_id=str(claimed.workspace_id),
        commit_sha=ctx.commit_sha,
        summaries=summaries,
        map_entries=generate.repo_map(files),
        readme=await _readme(rt, claimed, ctx),
        bounds=bounds,
        **_writer_request(ctx),
    )
    known = {f.file_id: (f.path, max(1, len(f.lines))) for f in files}
    output, status, provenance = await answer(
        rt, claimed, prepared, lambda o: generate.validate_plan(o, known, bounds)
    )
    if generate.validate_plan(output, known, bounds):  # even the fallback could not plan this scope
        output = {"pages": [], "reason": "insufficient evidence"}

    async def apply(db: AsyncSession) -> None:
        for ordinal, page in enumerate(output["pages"], start=1):
            await db.execute(
                text(
                    "INSERT INTO guide_pages(workspace_id, build_id, slug, title, ordinal, purpose, source_ranges)"
                    " VALUES (:w, :b, :slug, :title, :o, :purpose, CAST(:ranges AS jsonb))"
                    " ON CONFLICT (build_id, slug) DO NOTHING"
                ),
                {
                    "w": claimed.workspace_id,
                    "b": claimed.build,
                    "slug": page["slug"],
                    "title": page["title"],
                    "o": ordinal,
                    "purpose": page["purpose"],
                    "ranges": json.dumps(page["source_ranges"]),
                },
            )
        await _artifact(
            claimed,
            "guide_plan",
            await _store_json(
                rt, claimed, ctx, "guide-plan.json", {**output, "status": status, "provenance": provenance}
            ),
        )(db)

    return apply


# ---------------------------------------------------------------- guide pages
async def guide(rt: Any, claimed: Claim) -> Apply | None:
    ctx = await context(rt, claimed)
    files = {f.file_id: f for f in await load_files(rt, claimed, ctx)}
    pages = await _rows(
        rt,
        claimed,
        "SELECT id, slug, title, purpose, source_ranges FROM guide_pages WHERE workspace_id = :w AND build_id = :b ORDER BY ordinal",
    )
    texts = {fid: list(f.lines) for fid, f in files.items()}
    shas = {fid: f.sha256 for fid, f in files.items()}
    titles: dict[str, str] = {}
    work: list[Callable[[], Awaitable[Any]]] = []
    for page in pages:
        ranges = [r for r in page.source_ranges if r["file_id"] in files]
        evidence = generate.bounded_evidence(ranges, texts, shas)
        plan_entry = {"slug": page.slug, "title": page.title, "purpose": page.purpose}
        prepared = generate.guide_request(
            workspace_id=str(claimed.workspace_id),
            page=plan_entry,
            evidence=evidence,
            **_writer_request(ctx),
        )

        async def unit(
            page: Any = page,
            prepared: generate.Prepared = prepared,
            evidence: list[dict[str, Any]] = evidence,
        ) -> Any:
            if not evidence:
                return page, None, {"generator": "none", "error": "no_evidence"}
            output, _, provenance = await answer(
                rt, claimed, prepared, lambda o: generate.validate_guide(o, evidence)
            )
            if generate.validate_guide(output, evidence):
                return page, None, {"generator": "none", "error": "no_valid_page"}
            key = build_key(claimed.workspace_id, ctx.project_id, claimed.build, f"guide/{page.slug}.md")
            await rt.store.put(key, output["markdown"].encode(), "text/markdown; charset=utf-8")
            heading = generate.heading_text(output["markdown"])
            if heading:
                titles[page.slug] = heading[:200]  # a variant's page title, in its own language
            return page, key, provenance

        work.append(functools.partial(unit, page, prepared, evidence))
    results = await bounded(work)

    async def apply(db: AsyncSession) -> None:
        for page, key, provenance in results:
            await db.execute(
                text(
                    "UPDATE guide_pages SET status = :s, markdown_key = :k, provenance = CAST(:p AS jsonb),"
                    " title = COALESCE(CAST(:t AS text), title) WHERE workspace_id = :w AND id = :id"
                ),
                {
                    "s": "written" if key else "failed",
                    "k": key,
                    "p": json.dumps(provenance),
                    "t": titles.get(page.slug) if ctx.config.variant_of else None,
                    "w": claimed.workspace_id,
                    "id": page.id,
                },
            )

    return apply
