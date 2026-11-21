"""Export packages of a ready build (06 "/builds/{bid}/exports", 10, A19).

Both formats carry exactly what the viewer shows: text files of the snapshot that passed the scan
(excluded and blocked files are absent entirely), their definitions and cards, file explanations,
guide pages with their current claim checks, and the overview.

- kb_json: the knowledge base, validated against `kb-export.schema.json` before it is stored.
- static_zip: an offline reader: the product's own reading view, built from the frontend's `export.html`
  entry (`npm run build:export`, `frontend/dist-export`; the backend image builds it), reading the
  embedded knowledge base. Entry names come only from that build and fixed names (index.html, assets/,
  data/, NOTICE.txt, manifest.json): source files live inside the data, never as archive paths, so no
  name from the repository becomes a file name. The page loads nothing but its own files under a strict
  CSP.

The task runs on the ready build (stage `export`, work key = export id); its result is recorded in the
fenced completion, and a failure marks only the export as failed.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine import ENGINE_VERSION, PARSER_VERSION, generate
from app.engine.keys import build_key
from app.jobs.generation import _rows
from app.jobs.store import Apply, Claim

ASSETS = Path(__file__).resolve().parents[1] / "engine" / "export_assets"
# The built export viewer: EXPORT_VIEWER_DIR in the image, else the frontend's build in a checkout.
DEFAULT_VIEWER = Path(__file__).resolve().parents[3] / "frontend" / "dist-export"
KB_SCHEMA_ID = "urn:codehover-cloud:schema:kb-export:1.1.0"
EXPIRY_HOURS = 24
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)  # deterministic archives: same build, same bytes


class ExportError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@lru_cache(maxsize=1)
def kb_validator() -> Draft202012Validator:
    return Draft202012Validator(generate.schema("kb-export"))


async def collect(rt: Any, claimed: Claim, build_id: UUID | None = None) -> tuple[dict[str, Any], Any]:
    """The knowledge base of the claimed build, or of `build_id` (a language variant of it, same workspace)."""
    target = build_id or claimed.build

    async def rows(sql: str, **params: Any) -> list[Any]:
        return await _rows(rt, claimed, sql, b=target, **params)

    build = await rows(
        "SELECT b.project_id, b.snapshot_id, b.prompt_version, b.config, b.created_at, s.commit_sha,"
        " p.name AS project, r.owner || '/' || r.name AS repository FROM builds b"
        " JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
        " JOIN projects p ON p.workspace_id = b.workspace_id AND p.id = b.project_id"
        " JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
        " WHERE b.workspace_id = :w AND b.id = :b AND b.status = 'ready'",
    )
    if not build:
        raise ExportError("build_not_ready")
    meta = build[0]
    files = []
    for f in await rows(
        "SELECT f.id, f.path, f.language, f.sha256, f.blob_key, e.status, e.summary, e.chunks FROM source_files f"
        " LEFT JOIN file_explanations e ON e.workspace_id = f.workspace_id AND e.build_id = :b AND e.source_file_id = f.id"
        " WHERE f.workspace_id = :w AND f.snapshot_id = :s AND f.scan_status IN ('selected', 'unsupported')"
        " AND f.blob_key IS NOT NULL ORDER BY f.path",
        s=meta.snapshot_id,
    ):
        lines = (await rt.store.get(f.blob_key)).decode("utf-8").split("\n")
        explanation = (
            None if f.status is None else {"status": f.status, "summary": f.summary, "chunks": f.chunks}
        )
        files.append(
            {
                "id": str(f.id),
                "path": f.path,
                "language": f.language,
                "sha256": f.sha256,
                "lines": lines,
                "explanation": explanation,
                "references": [],
            }
        )
    by_file = {f["id"]: f for f in files}
    uses_at: dict[tuple[str, int, int], dict[str, Any]] = {}
    for e in await rows(
        'SELECT source_file_id, line, "column", resolution, ambiguity_group IS NOT NULL AS ambiguous, to_symbol_id'
        ' FROM reference_edges WHERE workspace_id = :w AND build_id = :b ORDER BY source_file_id, line, "column",'
        " to_symbol_id",
    ):
        if str(e.source_file_id) not in by_file:
            continue
        at = uses_at.setdefault(
            (str(e.source_file_id), e.line, e.column),
            {"line": e.line, "column": e.column, "targets": [], "resolution": "name", "ambiguous": False},
        )
        if str(e.to_symbol_id) not in at["targets"]:
            at["targets"].append(str(e.to_symbol_id))
        at["ambiguous"] = at["ambiguous"] or e.ambiguous or len(at["targets"]) > 1
        if e.resolution == "index":
            at["resolution"] = "index"
    for (file_id, _line, _column), use in uses_at.items():
        by_file[file_id]["references"].append(use)
    uses: dict[str, list[str]] = {}
    for e in await rows(
        "SELECT DISTINCT from_symbol_id, to_symbol_id FROM reference_edges"
        " WHERE workspace_id = :w AND build_id = :b AND from_symbol_id IS NOT NULL",
    ):
        uses.setdefault(str(e.from_symbol_id), []).append(str(e.to_symbol_id))
    symbols = [
        {
            "id": str(s.id),
            "file_id": str(s.source_file_id),
            "key": s.symbol_key,
            "name": s.name,
            "kind": s.kind,
            "start_line": s.start_line,
            "end_line": s.end_line,
            "signature": s.signature,
            "card": {k: v for k, v in (s.card or {}).items() if k != "source"},
            "uses": sorted(uses.get(str(s.id), [])),
        }
        for s in await rows(
            "SELECT id, source_file_id, symbol_key, name, kind, start_line, end_line, signature, card FROM symbols"
            " WHERE workspace_id = :w AND build_id = :b ORDER BY source_file_id, start_line, symbol_key",
        )
    ]
    claims: dict[str, list[dict[str, Any]]] = {}
    for c in await rows(
        "SELECT guide_page_id, claim_key, min(claim_text) AS text, min(ordinal) AS ordinal,"
        " CASE WHEN bool_or(status = 'invalid') THEN 'invalid' ELSE min(status) END AS status,"
        " COALESCE(min(reason) FILTER (WHERE reason <> ''), '') AS reason,"
        " COALESCE(bool_and(waived_at IS NOT NULL) FILTER (WHERE status <> 'invalid'), false) AS waived"
        " FROM claim_checks WHERE workspace_id = :w AND build_id = :b AND current"
        " GROUP BY guide_page_id, claim_key ORDER BY min(ordinal)",
    ):
        claims.setdefault(str(c.guide_page_id), []).append(
            {
                "key": c.claim_key,
                "text": c.text,
                "status": c.status,
                "reason": c.reason,
                "waived": bool(c.waived),
            }
        )
    guide = []
    for g in await rows(
        "SELECT id, slug, title, section, purpose, markdown_key, check_status FROM guide_pages"
        " WHERE workspace_id = :w AND build_id = :b AND status = 'written' ORDER BY ordinal",
    ):
        markdown = (await rt.store.get(g.markdown_key)).decode("utf-8")
        guide.append(
            {
                "slug": g.slug,
                "title": g.title,
                "section": g.section,
                "purpose": g.purpose,
                "markdown": markdown,
                "check_status": g.check_status,
                "claims": claims.get(str(g.id), []),
            }
        )
    overview_key = await rows(
        "SELECT object_key FROM artifacts WHERE workspace_id = :w AND build_id = :b AND kind = 'overview'",
    )
    overview = json.loads(await rt.store.get(overview_key[0].object_key)) if overview_key else None
    kb = {
        "schema": KB_SCHEMA_ID,
        "generator": {
            "engine": ENGINE_VERSION,
            "parser": PARSER_VERSION,
            "prompts": meta.prompt_version,
            "preset": meta.config.get("generation", {}).get("model_preset", "replay-v1"),
            "explanation_schema": generate.EXPLANATION_SCHEMA_ID,
        },
        "project": {"name": meta.project, "repository": meta.repository},
        "build": {
            "commit_sha": meta.commit_sha,
            "created_at": meta.created_at.isoformat(),
            "variant": "-".join(
                (
                    (meta.config.get("generation", {}).get("languages") or ["en"])[0],
                    (meta.config.get("generation", {}).get("levels") or ["standard"])[0],
                )
            ),
        },
        "files": files,
        "symbols": symbols,
        "guide": guide,
        "overview": overview,
    }
    errors = list(kb_validator().iter_errors(kb))
    if errors:
        raise ExportError("kb_schema_violation")
    return kb, meta


def viewer_files() -> dict[str, bytes]:
    """The built export viewer's files by archive name; its `export.html` becomes `index.html`."""
    root = Path(os.environ.get("EXPORT_VIEWER_DIR") or DEFAULT_VIEWER)
    if not (root / "export.html").is_file():
        raise ExportError("export_viewer_missing")
    files = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        name = path.relative_to(root).as_posix()
        if name.endswith(".map") or (name.endswith(".html") and name != "export.html"):
            continue
        files["index.html" if name == "export.html" else name] = path.read_bytes()
    return files


async def family_variants(rt: Any, claimed: Claim) -> list[UUID]:
    """The ready language variants of the claimed build's family at its reader level (FR-026), one per other
    language (the newest), which a static ZIP carries beside it."""
    rows = await _rows(
        rt,
        claimed,
        "WITH this AS (SELECT b.id, b.project_id,"
        "   COALESCE(CAST(b.config->'generation'->>'variant_of' AS uuid), b.id) AS family,"
        "   COALESCE(b.config->'generation'->'languages'->>0, 'en') AS language,"
        "   COALESCE(b.config->'generation'->'levels'->>0, 'standard') AS level"
        "   FROM builds b WHERE b.workspace_id = :w AND b.id = :b)"
        " SELECT DISTINCT ON (language) b.id, COALESCE(b.config->'generation'->'languages'->>0, 'en') AS language"
        " FROM builds b JOIN this t ON b.project_id = t.project_id AND b.id <> t.id"
        " WHERE b.workspace_id = :w AND b.status = 'ready' AND b.content_purged_at IS NULL"
        " AND COALESCE(b.config->'generation'->'levels'->>0, 'standard') = t.level"
        " AND COALESCE(b.config->'generation'->'languages'->>0, 'en') <> t.language"
        " AND (b.id = t.family OR b.config->'generation'->>'variant_of' = CAST(t.family AS text))"
        " ORDER BY language, b.created_at DESC",
    )
    return [r.id for r in rows]


def without_lines(kb: dict[str, Any]) -> dict[str, Any]:
    """A variant's knowledge base for the ZIP: its source lines are the main build's (the same snapshot), so
    they are left out and the reader takes them from there."""
    return {**kb, "files": [{**f, "lines": []} for f in kb["files"]]}


def static_zip(kb: dict[str, Any], variants: list[dict[str, Any]] | None = None) -> bytes:
    """Deterministic archive with fixed entry names only (10, 13 "safe filenames"). `variants` are the
    knowledge bases of the build's other languages (FR-026), without source lines."""
    data_js = "window.CODEHOVER_DATA = " + json.dumps(kb, ensure_ascii=True, sort_keys=True) + ";\n"
    if variants:
        data_js += (
            "window.CODEHOVER_VARIANTS = " + json.dumps(variants, ensure_ascii=True, sort_keys=True) + ";\n"
        )
    kb_json = json.dumps(kb, ensure_ascii=False, sort_keys=True, indent=1)
    entries: dict[str, bytes] = {
        **viewer_files(),
        "data/data.js": data_js.encode(),
        "data/kb.json": kb_json.encode(),
        "NOTICE.txt": (ASSETS / "NOTICE.txt").read_bytes(),
    }
    manifest = {
        "schema": "urn:codehover-cloud:schema:export-manifest:1.0.0",
        "kb_schema": KB_SCHEMA_ID,
        "commit_sha": kb["build"]["commit_sha"],
        "entries": {name: hashlib.sha256(body).hexdigest() for name, body in sorted(entries.items())},
    }
    entries["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=1).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entries[name])
    return buffer.getvalue()


async def export(rt: Any, claimed: Claim) -> Apply | None:
    export_id = UUID(claimed.work_key)
    found = await _rows(
        rt,
        claimed,
        "SELECT format, status FROM exports WHERE workspace_id = :w AND build_id = :b AND id = :e",
        e=export_id,
    )
    if not found or found[0].status != "pending":
        return None  # already finished (a duplicate delivery) or unknown
    try:
        kb, meta = await collect(rt, claimed)
        if found[0].format == "kb_json":
            body = json.dumps(kb, ensure_ascii=False, sort_keys=True).encode()
            name, content_type = f"exports/{export_id}.json", "application/json"
        else:
            variants = [
                without_lines((await collect(rt, claimed, v))[0]) for v in await family_variants(rt, claimed)
            ]
            body = static_zip(kb, variants)
            name, content_type = f"exports/{export_id}.zip", "application/zip"
    except ExportError as error:  # permanent: the export fails with its code, the build stays ready
        from app.jobs.stages import StageError  # stages imports this module

        raise StageError(error.code, transient=False) from error
    key = build_key(claimed.workspace_id, meta.project_id, claimed.build, name)
    sha = await rt.store.put(key, body, content_type)

    async def apply(db: AsyncSession) -> None:
        await db.execute(
            text(
                "UPDATE exports SET status = 'ready', object_key = :k, sha256 = :sha, byte_count = :n, completed_at = now(),"
                " expires_at = now() + make_interval(hours => :h) WHERE workspace_id = :w AND id = :e AND status = 'pending'"
            ),
            {
                "k": key,
                "sha": sha,
                "n": len(body),
                "h": EXPIRY_HOURS,
                "w": claimed.workspace_id,
                "e": export_id,
            },
        )

    return apply
