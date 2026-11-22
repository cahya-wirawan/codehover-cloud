"""Stage handlers: acquire -> index -> explain -> render (07 "Immutable pipeline").

Each handler reads what it needs and commits, performs external work (GitHub, sandbox, object storage)
with no transaction open, then returns an `apply` callback. The fenced `store.complete` runs it inside
its own transaction, so a worker that lost its lease never writes results. Object writes are keyed by
content or build and are idempotent; the manifest is uploaded last (04).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.github.app import GitHubApp, GitHubError
from app.adapters.github.public import PublicGitHub
from app.adapters.models.replay import ReplayGateway
from app.adapters.storage import ObjectStore
from app.core import metrics
from app.core.tenancy import set_context
from app.engine import ENGINE_VERSION, PARSER_VERSION, PROMPT_VERSION, indexers, sandbox, search
from app.engine.acquire import MAX_COMPRESSED_BYTES, ArchiveRejected, extract, extract_zip
from app.engine.catalog import Catalog
from app.engine.catalog import builtin as builtin_catalog
from app.engine.gateway import ModelGateway
from app.engine.index import IndexedSymbol, build_index
from app.engine.indexers import Config as IndexerConfig
from app.engine.keys import blob_key, build_key
from app.engine.replay import GENERATOR, explain_file
from app.engine.scan import scan
from app.jobs import exports, generation, verification
from app.jobs.store import Apply, Claim

logger = logging.getLogger("app.jobs.stages")
TEXT_STATUSES = ("selected", "unsupported")


class StageError(Exception):
    def __init__(self, code: str, *, transient: bool, apply: Apply | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient
        self.apply = apply


@dataclass(frozen=True)
class Runtime:
    sessions: async_sessionmaker[AsyncSession]
    store: ObjectStore
    github: GitHubApp | None
    allow_unsandboxed: bool = False
    catalog: Catalog = field(default_factory=builtin_catalog)
    # Provider name -> gateway. Only configured providers are present; replay is always available.
    gateways: dict[str, ModelGateway] = field(default_factory=lambda: {"replay": ReplayGateway()})
    retry_delays: tuple[float, ...] = (2.0, 8.0, 30.0)
    # Own-key builds (M6, T24): the sealer opens a workspace's key; the factory makes its gateway.
    sealer: Any = None
    own_key_gateway: Any = None  # Callable[[provider, key], ModelGateway]
    own_key_cache: dict[Any, Any] = field(default_factory=dict)
    # Public repositories without the App (anonymous source downloads); None refuses them.
    public_github: PublicGitHub | None = None
    # SCIP indexers (T23); None keeps every reference on name matching.
    indexers: IndexerConfig | None = None


async def _read(rt: Runtime, claimed: Claim, sql: str, **params: Any) -> list[Any]:
    async with rt.sessions() as db:
        await set_context(db, workspace_id=claimed.workspace_id)
        rows = list(await db.execute(text(sql), {"w": claimed.workspace_id, "b": claimed.build_id, **params}))
        await db.commit()
    return rows


def _github_error(error: GitHubError) -> StageError:
    transient = error.status == 0 or error.status >= 500 or error.reason == "rate_limited"
    return StageError(f"github_{error.reason}", transient=transient)


# ---------------------------------------------------------------- acquire
_SOURCE = (
    "SELECT s.project_id, s.id AS snapshot_id, s.status, s.commit_sha, s.archive_key, r.provider_repo_id,"
    " r.access_revoked_at, c.installation_id, c.status AS connection_status, c.provider"
    " FROM snapshots s"
    " JOIN projects p ON p.workspace_id = s.workspace_id AND p.id = s.project_id"
    " JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
    " JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id"
)


async def acquire(rt: Runtime, claimed: Claim) -> Apply | None:
    """Download, extract and scan the snapshot: for a build (M1 replay builds) or on its own (0006)."""
    if claimed.snapshot_id is not None:
        rows = await _read(
            rt, claimed, _SOURCE + " WHERE s.workspace_id = :w AND s.id = :s", s=claimed.snapshot_id
        )
    else:
        rows = await _read(
            rt,
            claimed,
            _SOURCE + " JOIN builds b ON b.workspace_id = s.workspace_id AND b.snapshot_id = s.id"
            " WHERE b.workspace_id = :w AND b.id = :b",
        )
    row = rows[0]
    if row.status == "scanned":
        return None  # immutable snapshot already acquired (cache across builds)
    uploaded = row.provider == "upload"  # M6: the archive was uploaded and stored with the snapshot
    if row.connection_status != "active" or row.access_revoked_at is not None:
        raise StageError("repository_access_revoked", transient=False)
    if uploaded:
        if row.archive_key is None:
            raise StageError("upload_missing", transient=False)
        archive = await rt.store.get(row.archive_key)
    elif row.provider == "public":  # anonymous download of a repository that must still be public
        if rt.public_github is None:
            raise StageError("repository_access_revoked", transient=False)
        try:
            archive = await rt.public_github.download_tarball(
                row.provider_repo_id, row.commit_sha, MAX_COMPRESSED_BYTES
            )
        except GitHubError as error:
            if error.reason == "not_found":
                raise StageError("repository_not_public", transient=False) from error
            raise _github_error(error) from error
    else:
        if rt.github is None:
            raise StageError("repository_access_revoked", transient=False)
        try:
            archive = await rt.github.download_tarball(
                row.installation_id, row.provider_repo_id, row.commit_sha, MAX_COMPRESSED_BYTES
            )
        except GitHubError as error:
            raise _github_error(error) from error
    try:
        if uploaded:
            extraction = await asyncio.to_thread(extract_zip, archive)
        else:
            extraction = await asyncio.to_thread(extract, io.BytesIO(archive))
    except ArchiveRejected as rejected:
        rejection_code = (
            rejected.code
        )  # the `as` name is cleared when this block ends; the closure runs later

        async def mark_rejected(db: AsyncSession) -> None:
            await db.execute(
                text(
                    "UPDATE snapshots SET status = 'rejected', rejection_code = :c, updated_at = now()"
                    " WHERE workspace_id = :w AND id = :s"
                ),
                {"c": rejection_code, "w": claimed.workspace_id, "s": row.snapshot_id},
            )

        raise StageError(f"archive_{rejection_code}", transient=False, apply=mark_rejected) from rejected
    files = scan(extraction.entries)
    for f in files:
        if f.text is not None and f.sha256:
            await rt.store.put(
                blob_key(claimed.workspace_id, row.project_id, row.snapshot_id, f.sha256),
                f.text.encode(),
                "text/plain; charset=utf-8",
            )
    digest = hashlib.sha256(
        "".join(f"{f.path}\0{f.sha256 or ''}\0{f.status}\n" for f in files).encode()
    ).hexdigest()

    async def apply(db: AsyncSession) -> None:
        for f in files:
            await db.execute(
                text(
                    "INSERT INTO source_files(workspace_id, snapshot_id, path, language, sha256, blob_key,"
                    " line_count, byte_count, scan_status, scan_reason)"
                    " VALUES (:w, :s, :path, :lang, :sha, :key, :lines, :bytes, :status, :reason)"
                    " ON CONFLICT (snapshot_id, path) DO NOTHING"
                ),
                {
                    "w": claimed.workspace_id,
                    "s": row.snapshot_id,
                    "path": f.path,
                    "lang": f.language,
                    "sha": f.sha256,
                    "lines": f.line_count,
                    "bytes": f.byte_count,
                    "status": f.status,
                    "reason": f.reason,
                    "key": blob_key(claimed.workspace_id, row.project_id, row.snapshot_id, f.sha256)
                    if f.text is not None and f.sha256
                    else None,
                },
            )
        await db.execute(
            text(
                "UPDATE snapshots SET status = 'scanned', digest = :d, source_bytes = :n, file_count = :c,"
                " updated_at = now() WHERE workspace_id = :w AND id = :s"
            ),
            {
                "d": digest,
                "n": extraction.expanded_bytes,
                "c": len(files),
                "w": claimed.workspace_id,
                "s": row.snapshot_id,
            },
        )

    return apply


# ---------------------------------------------------------------- index (sandboxed parse)
async def index(rt: Runtime, claimed: Claim) -> Apply | None:
    rows = await _read(
        rt,
        claimed,
        "SELECT f.id, f.path, f.language, f.blob_key FROM builds b"
        " JOIN source_files f ON f.workspace_id = b.workspace_id AND f.snapshot_id = b.snapshot_id"
        " WHERE b.workspace_id = :w AND b.id = :b AND f.scan_status = 'selected' ORDER BY f.path",
    )
    payload = {
        "files": [
            {
                "file_id": str(r.id),
                "path": r.path,
                "language": r.language,
                "text": (await rt.store.get(r.blob_key)).decode("utf-8"),
            }
            for r in rows
        ]
    }
    try:
        parsed = await sandbox.run("app.engine.parse_worker", payload, allow_unsandboxed=rt.allow_unsandboxed)
    except sandbox.SandboxError as error:
        raise StageError(f"sandbox_{error.reason}", transient=error.reason == "timeout") from error
    resolved: dict[str, dict[str, Any]] = {}
    if rt.indexers is not None:
        manifests = []
        if any(r.language == "rust" for r in rows):  # Cargo manifests give rust-analyzer the crate graph
            manifest_rows = await _read(
                rt,
                claimed,
                "SELECT f.path, f.blob_key FROM builds b"
                " JOIN source_files f ON f.workspace_id = b.workspace_id AND f.snapshot_id = b.snapshot_id"
                " WHERE b.workspace_id = :w AND b.id = :b AND f.blob_key IS NOT NULL"
                " AND (f.path = 'Cargo.toml' OR f.path LIKE '%/Cargo.toml') ORDER BY f.path LIMIT :n",
                n=indexers.MAX_MANIFESTS,
            )
            manifests = [
                {"path": m.path, "text": (await rt.store.get(m.blob_key)).decode("utf-8")}
                for m in manifest_rows
            ]
        tsconfigs = []
        if any(r.language in indexers.FAMILIES["scip-typescript"] for r in rows):  # path aliases (07)
            config_rows = await _read(
                rt,
                claimed,
                "SELECT f.path, f.blob_key FROM builds b"
                " JOIN source_files f ON f.workspace_id = b.workspace_id AND f.snapshot_id = b.snapshot_id"
                " WHERE b.workspace_id = :w AND b.id = :b AND f.blob_key IS NOT NULL"
                " AND f.path ~ '(^|/)(tsconfig|jsconfig)[^/]*[.]json$' ORDER BY f.path LIMIT :n",
                n=indexers.MAX_TSCONFIGS,
            )
            tsconfigs = [
                {"path": c.path, "text": (await rt.store.get(c.blob_key)).decode("utf-8")}
                for c in config_rows
            ]
        resolved, outcomes = await indexers.resolve(
            rt.indexers,
            payload["files"],
            allow_unsandboxed=rt.allow_unsandboxed,
            manifests=manifests,
            tsconfigs=tsconfigs,
        )
        for outcome in outcomes:
            metrics.INDEXER_RUNS.labels(indexer=outcome.indexer, outcome=outcome.status).inc()
            logger.info(
                "reference indexer finished",
                extra={
                    "build_id": str(claimed.build_id),
                    "indexer": outcome.indexer,
                    "files": outcome.files,
                    "status": outcome.status,
                    "reason": outcome.reason,
                },
            )
    graph = build_index(parsed["files"], {str(r.id): r.path for r in rows}, resolved)

    async def apply(db: AsyncSession) -> None:
        ids: dict[str, UUID] = {}
        for key, symbol in graph.symbols.items():
            data = symbol.data
            ids[key] = (
                await db.execute(
                    text(
                        "INSERT INTO symbols(workspace_id, build_id, source_file_id, symbol_key, name, kind,"
                        " parent_key, start_line, end_line, start_column, end_column, signature, rank, card)"
                        " VALUES (:w, :b, :f, :k, :n, :kind, :parent, :sl, :el, :sc, :ec, :sig, :rank,"
                        " CAST(:card AS jsonb)) RETURNING id"
                    ),
                    {
                        "w": claimed.workspace_id,
                        "b": claimed.build_id,
                        "f": UUID(symbol.file_id),
                        "k": data["symbol_key"],
                        "n": data["name"],
                        "kind": data["kind"],
                        "parent": data["parent_key"],
                        "sl": data["start_line"],
                        "el": data["end_line"],
                        "sc": data["start_column"],
                        "ec": data["end_column"],
                        "sig": data["signature"],
                        "rank": symbol.rank,
                        "card": json.dumps(
                            {"source": {"docstring": data.get("docstring"), "params": data.get("params", [])}}
                        ),
                    },
                )
            ).scalar_one()
        for edge in graph.edges:
            await db.execute(
                text(
                    "INSERT INTO reference_edges(workspace_id, build_id, from_symbol_id, to_symbol_id,"
                    ' source_file_id, line, "column", resolution, ambiguity_group)'
                    " VALUES (:w, :b, :src, :dst, :f, :line, :col, :resolution, :group)"
                ),
                {
                    "w": claimed.workspace_id,
                    "b": claimed.build_id,
                    "src": ids[edge.from_key] if edge.from_key else None,
                    "dst": ids[edge.to_key],
                    "f": UUID(edge.file_id),
                    "line": edge.line,
                    "col": edge.column,
                    "resolution": edge.resolution,
                    "group": edge.ambiguity_group,
                },
            )

    return apply


async def _is_generation(rt: Runtime, claimed: Claim) -> bool:
    rows = await _read(
        rt, claimed, "SELECT config ? 'generation' AS g FROM builds WHERE workspace_id = :w AND id = :b"
    )
    return bool(rows[0].g)


def _generation(
    handler: Callable[[Runtime, Claim], Awaitable[Apply | None]],
) -> Callable[[Runtime, Claim], Awaitable[Apply | None]]:
    async def run(rt: Runtime, claimed: Claim) -> Apply | None:
        try:
            return await handler(rt, claimed)
        except generation.StageStop as stop:
            raise StageError(stop.code, transient=stop.code == "provider_unavailable") from stop

    return run


# ---------------------------------------------------------------- explain (replay)
async def explain(rt: Runtime, claimed: Claim) -> Apply | None:
    if await _is_generation(rt, claimed):
        return await _generation(generation.explain)(rt, claimed)
    rows = await _read(
        rt,
        claimed,
        "SELECT s.id, s.source_file_id, f.path, s.symbol_key, s.name, s.kind, s.signature, s.card"
        " FROM symbols s JOIN source_files f ON f.workspace_id = s.workspace_id AND f.id = s.source_file_id"
        " WHERE s.workspace_id = :w AND s.build_id = :b ORDER BY f.path, s.start_line",
    )
    by_file: dict[str, list[tuple[Any, IndexedSymbol]]] = {}
    for r in rows:
        source = (r.card or {}).get("source", {})
        data = {
            "symbol_key": r.symbol_key,
            "name": r.name,
            "kind": r.kind,
            "signature": r.signature,
            "docstring": source.get("docstring"),
            "params": source.get("params", []),
        }
        by_file.setdefault(r.path, []).append((r, IndexedSymbol(str(r.source_file_id), r.path, data)))
    cards: dict[UUID, dict[str, Any]] = {}
    for path, items in by_file.items():
        explanation = explain_file(path, [symbol for _, symbol in items])
        for (r, _), item_card in zip(items, explanation["symbols"], strict=False):
            cards[r.id] = {
                **item_card,
                "provenance": {"generator": GENERATOR, "prompt_version": PROMPT_VERSION},
            }

    async def apply(db: AsyncSession) -> None:
        for symbol_id, value in cards.items():
            await db.execute(
                text("UPDATE symbols SET card = CAST(:c AS jsonb) WHERE workspace_id = :w AND id = :id"),
                {"c": json.dumps(value), "w": claimed.workspace_id, "id": symbol_id},
            )

    return apply


# ---------------------------------------------------------------- render (manifest last)
async def render(rt: Runtime, claimed: Claim) -> Apply | None:
    build = (
        await _read(
            rt,
            claimed,
            "SELECT b.project_id, b.snapshot_id, b.config, b.prompt_version, s.commit_sha, s.digest,"
            " (SELECT count(*) FROM symbols y WHERE y.workspace_id = b.workspace_id AND y.build_id = b.id) AS symbols,"
            " (SELECT count(*) FROM reference_edges e WHERE e.workspace_id = b.workspace_id AND e.build_id = b.id) AS edges,"
            " (SELECT count(*) FROM reference_edges e WHERE e.workspace_id = b.workspace_id AND e.build_id = b.id"
            " AND e.resolution = 'index') AS index_edges"
            " FROM builds b JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
            " WHERE b.workspace_id = :w AND b.id = :b",
        )
    )[0]
    files = await _read(
        rt,
        claimed,
        "SELECT f.id, f.path, f.sha256, f.scan_status FROM builds b"
        " JOIN source_files f ON f.workspace_id = b.workspace_id AND f.snapshot_id = b.snapshot_id"
        " WHERE b.workspace_id = :w AND b.id = :b ORDER BY f.path",
    )
    manifest = {
        "schema": "urn:codehover-cloud:schema:build-manifest:0.1.0",
        "build_id": str(claimed.build_id),
        "commit_sha": build.commit_sha,
        "snapshot_digest": build.digest,
        "engine_version": ENGINE_VERSION,
        "parser_version": PARSER_VERSION,
        "prompt_version": build.prompt_version,
        "generator": build.config.get("generation", {}).get("model_preset", GENERATOR),
        "catalog": build.config.get("catalog"),
        "explanation_schema": "urn:codehover-cloud:schema:explanation:2.0.0",
        "counts": {
            "files": len(files),
            "symbols": build.symbols,
            "edges": build.edges,
            "edges_by_resolution": {"index": build.index_edges, "name": build.edges - build.index_edges},
        },
        # T23: indexers available to this worker; per-edge `resolution` says which references they resolved.
        "indexers": indexers.versions(rt.indexers) if rt.indexers is not None else {},
        "guide": [
            {"slug": g.slug, "title": g.title, "status": g.status, "markdown_key": g.markdown_key}
            for g in await _read(
                rt,
                claimed,
                "SELECT slug, title, status, markdown_key FROM guide_pages WHERE workspace_id = :w AND build_id = :b"
                " ORDER BY ordinal",
            )
        ],
        "artifacts": {
            a.kind: {"key": a.object_key, "sha256": a.sha256}
            for a in await _read(
                rt,
                claimed,
                "SELECT kind, object_key, sha256 FROM artifacts WHERE workspace_id = :w AND build_id = :b"
                " AND kind <> 'manifest' ORDER BY kind",
            )
        },
        "files": [
            {"id": str(f.id), "path": f.path, "sha256": f.sha256, "status": f.scan_status} for f in files
        ],
    }
    documents = await _search_documents(rt, claimed)
    body = json.dumps(manifest, sort_keys=True).encode()
    key = build_key(claimed.workspace_id, build.project_id, claimed.build, "manifest.json")
    sha = await rt.store.put(key, body, "application/json")

    async def apply(db: AsyncSession) -> None:
        await db.execute(
            text("DELETE FROM search_documents WHERE workspace_id = :w AND build_id = :b"),
            {"w": claimed.workspace_id, "b": claimed.build},
        )
        for doc in documents:
            await db.execute(
                text(
                    "INSERT INTO search_documents(workspace_id, build_id, kind, ref_id, name, path, line, symbol_kind,"
                    " snippet, terms, body) VALUES (:w, :b, :kind, :ref, :name, :path, :line, :sk, :snippet, :terms, :body)"
                ),
                {"w": claimed.workspace_id, "b": claimed.build, **doc},
            )
        await db.execute(
            text(
                "INSERT INTO artifacts(workspace_id, build_id, kind, object_key, sha256, byte_count, content_type)"
                " VALUES (:w, :b, 'manifest', :k, :sha, :n, 'application/json')"
                " ON CONFLICT (build_id, kind, variant) DO NOTHING"
            ),
            {"w": claimed.workspace_id, "b": claimed.build_id, "k": key, "sha": sha, "n": len(body)},
        )
        await db.execute(
            text("UPDATE builds SET manifest_key = :k WHERE workspace_id = :w AND id = :b"),
            {"k": key, "w": claimed.workspace_id, "b": claimed.build_id},
        )

    return apply


async def _search_documents(rt: Runtime, claimed: Claim) -> list[dict[str, Any]]:
    """Definitions, files and guide pages of this build, for build-scoped search (05)."""
    documents: list[dict[str, Any]] = []
    for s in await _read(
        rt,
        claimed,
        "SELECT y.id, y.name, y.symbol_key, y.kind, y.start_line, y.signature, y.card->>'summary' AS summary,"
        " f.path FROM symbols y JOIN source_files f ON f.workspace_id = y.workspace_id AND f.id = y.source_file_id"
        " WHERE y.workspace_id = :w AND y.build_id = :b",
    ):
        documents.append(
            {
                "kind": "symbol",
                "ref": s.id,
                "name": s.name[:1024],
                "path": s.path,
                "line": s.start_line,
                "sk": s.kind,
                "snippet": (s.summary or s.signature or "")[:600],
                "terms": search.identifier_terms(s.name, s.symbol_key),
                "body": f"{s.signature or ''} {s.summary or ''}"[:20000],
            }
        )
    for f in await _read(
        rt,
        claimed,
        "SELECT f.id, f.path, e.summary FROM builds b JOIN source_files f"
        " ON f.workspace_id = b.workspace_id AND f.snapshot_id = b.snapshot_id"
        " LEFT JOIN file_explanations e ON e.workspace_id = f.workspace_id AND e.build_id = b.id AND e.source_file_id = f.id"
        " WHERE b.workspace_id = :w AND b.id = :b AND f.scan_status IN ('selected', 'unsupported')",
    ):
        name = f.path.rsplit("/", 1)[-1]
        documents.append(
            {
                "kind": "file",
                "ref": f.id,
                "name": name[:1024],
                "path": f.path,
                "line": None,
                "sk": None,
                "snippet": (f.summary or "")[:600],
                "terms": search.identifier_terms(f.path),
                "body": (f.summary or "")[:20000],
            }
        )
    for g in await _read(
        rt,
        claimed,
        "SELECT id, slug, title, purpose, markdown_key FROM guide_pages"
        " WHERE workspace_id = :w AND build_id = :b AND status = 'written'",
    ):
        markdown = (await rt.store.get(g.markdown_key)).decode("utf-8") if g.markdown_key else ""
        documents.append(
            {
                "kind": "guide",
                "ref": g.id,
                "name": g.title[:1024],
                "path": g.slug,
                "line": None,
                "sk": None,
                "snippet": (g.purpose or "")[:600],
                "terms": search.identifier_terms(g.slug),
                "body": search.prose(markdown),
            }
        )
    return documents


HANDLERS = {
    "acquire": acquire,
    "index": index,
    "explain": explain,
    "overview": _generation(generation.overview),
    "plan": _generation(generation.plan),
    "guide": _generation(generation.guide),
    "verify": _generation(verification.verify),
    "check": _generation(verification.check),
    "render": render,
    "export": exports.export,  # runs on a ready build (0013)
}
