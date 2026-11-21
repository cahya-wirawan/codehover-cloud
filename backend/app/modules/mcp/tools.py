"""The hosted MCP tools: the same eight as codehover's local server, over one published site's build.

Every tool reads through the viewer's service functions with the site's reader (workspace from the
resolver), so it sees exactly what the site shows: the published build or an approved language variant.
Results are plain text (Markdown). Repository content in results is data for the client's model; the
server never follows it. Service functions commit between database reads and object storage reads, so
the tenant context is set again before each one.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import ObjectStore
from app.core.errors import ApiError
from app.core.tenancy import set_context
from app.modules.projects import service
from app.modules.published.router import Site, site_variants

MAX_LINES = 400
MAX_DEFINITION_LINES = 200
MAX_REFERENCES = 100
MAX_TEXT = 120_000
CITATION = re.compile(r"\[\[([0-9a-f-]{36}):(\d+)-(\d+)\]\]")
Language = str | None


class ToolError(Exception):
    """A tool's own failure, returned to the client as an error result (`isError`), not a protocol error."""


@dataclass
class Context:
    db: AsyncSession
    objects: ObjectStore
    site: Site
    build_id: UUID

    @property
    def workspace_id(self) -> UUID:
        return self.site.reader.workspace_id

    async def ready(self) -> None:
        await set_context(self.db, workspace_id=self.workspace_id)

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        await self.ready()
        result = list(
            await self.db.execute(text(sql), {"w": self.workspace_id, "b": self.build_id, **params})
        )
        await self.db.commit()
        return result


# ---------------------------------------------------------------- arguments
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str | None = Field(default=None, pattern=r"^(en|de|id)$")


class NoArgs(_Args):
    pass


class SearchArgs(_Args):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)


class DefinitionArgs(_Args):
    name: str = Field(min_length=1, max_length=600)
    include_source: bool = True


class NameArgs(_Args):
    name: str = Field(min_length=1, max_length=600)


class PathArgs(_Args):
    path: str = Field(min_length=1, max_length=1024)


class LinesArgs(_Args):
    path: str = Field(min_length=1, max_length=1024)
    start: int = Field(default=1, ge=1)
    end: int | None = Field(default=None, ge=1)


class GuideArgs(_Args):
    slug: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


_LANGUAGE = {
    "type": "string",
    "enum": ["en", "de", "id"],
    "description": "read the site's variant in this language, when it has one",
}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**properties, "language": _LANGUAGE},
        "required": required,
        "additionalProperties": False,
    }


# ---------------------------------------------------------------- helpers
def _clip(value: str) -> str:
    return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "\n\n[truncated]"


async def _paths(ctx: Context) -> dict[str, str]:
    rows = await ctx.rows(
        "SELECT f.id, f.path FROM builds b JOIN source_files f"
        " ON f.workspace_id = b.workspace_id AND f.snapshot_id = b.snapshot_id"
        " WHERE b.workspace_id = :w AND b.id = :b"
    )
    return {str(r.id): r.path for r in rows}


def _cite(markdown: str, paths: dict[str, str]) -> str:
    """`[[file_id:a-b]]` markers become `path:a-b`, which a reader (or read_lines) can follow."""

    def one(match: re.Match[str]) -> str:
        path = paths.get(match.group(1), "?")
        a, b = match.group(2), match.group(3)
        return f"(`{path}:{a}`)" if a == b else f"(`{path}:{a}-{b}`)"

    return CITATION.sub(one, markdown)


async def _file_id(ctx: Context, path: str) -> UUID:
    rows = await ctx.rows(
        "SELECT f.id FROM builds b JOIN source_files f"
        " ON f.workspace_id = b.workspace_id AND f.snapshot_id = b.snapshot_id"
        " WHERE b.workspace_id = :w AND b.id = :b AND f.path = :p",
        p=path.strip().removeprefix("./").strip("/"),
    )
    if not rows:
        raise ToolError(f"No file {path!r} in this site. Use search() to find files.")
    return UUID(str(rows[0].id))


async def _symbol(ctx: Context, name: str) -> Any:
    """One definition by id, `path::key`, key (`Class.method`) or bare name. Several equally good
    matches are an error that lists them, so the caller can choose (07 "explicit ambiguity")."""
    rows = await ctx.rows(
        "SELECT y.id, y.symbol_key, y.name, y.kind, y.start_line, f.path,"
        " CASE WHEN CAST(y.id AS text) = :n OR f.path || '::' || y.symbol_key = :n THEN 0"
        "   WHEN y.symbol_key = :n THEN 1 ELSE 2 END AS tier"
        " FROM symbols y JOIN source_files f ON f.workspace_id = y.workspace_id AND f.id = y.source_file_id"
        " WHERE y.workspace_id = :w AND y.build_id = :b AND (CAST(y.id AS text) = :n"
        " OR f.path || '::' || y.symbol_key = :n OR y.symbol_key = :n OR y.name = :n)"
        " ORDER BY tier, f.path, y.start_line LIMIT 21",
        n=name.strip(),
    )
    if not rows:
        raise ToolError(f"No definition named {name!r}. Use search() to find it.")
    best = [r for r in rows if r.tier == rows[0].tier]
    if len(best) > 1:
        listed = "\n".join(f"- `{r.path}::{r.symbol_key}` ({r.kind}, line {r.start_line})" for r in best[:20])
        raise ToolError(f"{name!r} names several definitions; ask again with one of these ids:\n{listed}")
    return best[0]


def _numbered(lines: list[str], first: int, notes: dict[int, list[str]] | None = None) -> str:
    width = len(str(first + len(lines)))
    out = []
    for offset, line in enumerate(lines):
        number = first + offset
        out.append(f"{number:>{width}} | {line}")
        for note in (notes or {}).get(number, []):
            out.append(f"{'':>{width}} ↳ {note}")
    return "\n".join(out)


# ---------------------------------------------------------------- tools
async def overview(ctx: Context, args: NoArgs) -> str:
    info = (
        await ctx.rows(
            "SELECT p.name, r.owner || '/' || r.name AS repository, s.commit_sha FROM builds b"
            " JOIN projects p ON p.workspace_id = b.workspace_id AND p.id = b.project_id"
            " JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id"
            " JOIN snapshots s ON s.workspace_id = b.workspace_id AND s.id = b.snapshot_id"
            " WHERE b.workspace_id = :w AND b.id = :b"
        )
    )[0]
    await ctx.ready()
    index = await service.guide_index(ctx.db, ctx.objects, ctx.site.reader, ctx.build_id)
    paths = await _paths(ctx)
    parts = [f"# {info.name}", f"Repository {info.repository} at commit {info.commit_sha[:12]}."]
    data = index.get("overview") or {}
    if data.get("overview"):
        parts.append(_cite(str(data["overview"]), paths))
    if data.get("key_concepts"):
        parts.append(
            "## Key concepts\n"
            + "\n".join(f"- **{c.get('term')}**: {c.get('explanation')}" for c in data["key_concepts"])
        )
    if data.get("reading_order"):
        parts.append(
            "## Reading order\n"
            + "\n".join(
                f"{n}. `{item.get('path') or '?'}`: {item.get('reason')}"
                for n, item in enumerate(data["reading_order"], 1)
            )
        )
    if index["pages"]:
        parts.append("## Guide pages\n" + "\n".join(f"- `{p['slug']}`: {p['title']}" for p in index["pages"]))
    parts.append(
        "Use search() and get_definition() to look things up, read_guide_page() for topics, and"
        " read_lines() to confirm against the source: the explanations are machine-written."
    )
    return "\n\n".join(parts)


async def search(ctx: Context, args: SearchArgs) -> str:
    await ctx.ready()
    hits = await service.search_build(ctx.db, ctx.site.reader, ctx.build_id, args.query, None, args.limit)
    if not hits:
        return f"Nothing found for {args.query!r}."
    lines = []
    for h in hits:
        snippet = f": {h['snippet']}" if h.get("snippet") else ""
        if h["kind"] == "symbol":
            lines.append(
                f"- {h.get('symbol_kind') or 'definition'} `{h['name']}` in `{h['path']}:{h['line']}`{snippet}"
            )
        elif h["kind"] == "file":
            lines.append(f"- file `{h['path']}`{snippet}")
        else:
            lines.append(f"- guide page `{h['path']}`: {h['name']}{snippet}")
    return "\n".join(lines)


async def get_definition(ctx: Context, args: DefinitionArgs) -> str:
    found = await _symbol(ctx, args.name)
    await ctx.ready()
    view = await service.symbol_view(ctx.db, ctx.site.reader, ctx.build_id, found.id)
    card: dict[str, Any] = view.get("card") or {}
    parts = [
        f"# {view['kind']} `{view['name']}`",
        f"`{view['path']}:{view['start_line']}-{view['end_line']}` (id `{view['path']}::{view['symbol_key']}`)",
    ]
    if view.get("signature"):
        parts.append(f"```\n{view['signature']}\n```")
    for key in ("summary", "details"):
        if card.get(key):
            parts.append(str(card[key]))
    if card.get("params"):
        parts.append(
            "## Parameters\n"
            + "\n".join(f"- `{p.get('name')}`: {p.get('description')}" for p in card["params"])
        )
    if card.get("returns"):
        parts.append(f"## Returns\n{card['returns']}")
    if card.get("notes"):
        parts.append(f"## Notes\n{card['notes']}")
    for title, key in (("Uses", "uses"), ("Used by", "used_by")):
        if view.get(key):
            parts.append(
                f"## {title}\n"
                + "\n".join(
                    f"- `{r['name']}` ({r['kind']}) in `{r['path']}:{r['start_line']}`" for r in view[key]
                )
            )
    if view.get("guides"):
        parts.append("## Guide pages\n" + "\n".join(f"- `{g['slug']}`: {g['title']}" for g in view["guides"]))
    if args.include_source:
        end = min(view["end_line"], view["start_line"] + MAX_DEFINITION_LINES - 1)
        await ctx.ready()
        source = await service.file_view(
            ctx.db, ctx.objects, ctx.site.reader, ctx.build_id, view["file_id"], view["start_line"], end
        )
        more = "\n…" if end < view["end_line"] else ""
        parts.append(f"## Source\n```\n{_numbered(source['lines'], source['start_line'])}{more}\n```")
    return "\n\n".join(parts)


async def find_references(ctx: Context, args: NameArgs) -> str:
    found = await _symbol(ctx, args.name)
    rows = await ctx.rows(
        "SELECT f.path, f.blob_key, e.line, e.resolution, o.name AS caller FROM reference_edges e"
        " JOIN source_files f ON f.workspace_id = e.workspace_id AND f.id = e.source_file_id"
        " LEFT JOIN symbols o ON o.workspace_id = e.workspace_id AND o.id = e.from_symbol_id"
        " WHERE e.workspace_id = :w AND e.build_id = :b AND e.to_symbol_id = :s"
        " ORDER BY f.path, e.line LIMIT :n",
        s=found.id,
        n=MAX_REFERENCES + 1,
    )
    if not rows:
        return f"No uses of `{found.path}::{found.symbol_key}` were found."
    texts: dict[str, list[str]] = {}
    for key in {r.blob_key for r in rows if r.blob_key}:  # no transaction open (04)
        texts[key] = (await ctx.objects.get(key)).decode("utf-8", "replace").split("\n")
    seen: set[tuple[str, int]] = set()
    out = [f"Uses of `{found.path}::{found.symbol_key}`:"]
    for r in rows[:MAX_REFERENCES]:
        if (r.path, r.line) in seen:
            continue
        seen.add((r.path, r.line))
        lines = texts.get(r.blob_key) or []
        code = lines[r.line - 1].strip() if 0 < r.line <= len(lines) else ""
        where = f" in `{r.caller}`" if r.caller else ""
        label = "" if r.resolution == "index" else " (matched by name)"
        out.append(f"- `{r.path}:{r.line}`{where}{label}: `{code[:200]}`")
    if len(rows) > MAX_REFERENCES:
        out.append(f"(first {MAX_REFERENCES} shown)")
    return "\n".join(out)


async def get_file(ctx: Context, args: PathArgs) -> str:
    file_id = await _file_id(ctx, args.path)
    await ctx.ready()
    view = await service.file_view(ctx.db, ctx.objects, ctx.site.reader, ctx.build_id, file_id, 1, 1)
    parts = [f"# `{view['path']}`", f"{view['language'] or 'text'}, {view['line_count']} lines."]
    explanation = view.get("explanation") or {}
    if explanation.get("summary"):
        parts.append(str(explanation["summary"]))
    if view["symbols"]:
        parts.append(
            "## Definitions\n"
            + "\n".join(
                f"- {s['kind']} `{s['symbol_key']}` (lines {s['start_line']}-{s['end_line']})"
                + (f": {s['summary']}" if s.get("summary") else "")
                for s in view["symbols"]
            )
        )
    return "\n\n".join(parts)


async def read_lines(ctx: Context, args: LinesArgs) -> str:
    file_id = await _file_id(ctx, args.path)
    end = min(args.end or args.start + MAX_LINES - 1, args.start + MAX_LINES - 1)
    if end < args.start:
        raise ToolError("`end` must not be before `start`.")
    await ctx.ready()
    view = await service.file_view(
        ctx.db, ctx.objects, ctx.site.reader, ctx.build_id, file_id, args.start, end
    )
    if not view["lines"]:
        raise ToolError(f"`{view['path']}` has {view['line_count']} lines.")
    notes: dict[int, list[str]] = {}
    for chunk in (view.get("explanation") or {}).get("chunks") or []:
        for note in chunk.get("notes") or []:
            if isinstance(note, dict) and note.get("text"):
                notes.setdefault(int(note.get("end_line") or note.get("start_line") or 0), []).append(
                    str(note["text"])
                )
    header = f"`{view['path']}` lines {view['start_line']}-{view['end_line']} of {view['line_count']}"
    return f"{header}\n```\n{_numbered(view['lines'], view['start_line'], notes)}\n```"


async def list_guide(ctx: Context, args: NoArgs) -> str:
    await ctx.ready()
    index = await service.guide_index(ctx.db, ctx.objects, ctx.site.reader, ctx.build_id)
    if not index["pages"]:
        return "This site has no guide pages."
    out: list[str] = []
    section: str | None = None
    for page in index["pages"]:
        if (page.get("section") or "") != section:
            section = page.get("section") or ""
            if section:
                out.append(f"\n## {section}")
        purpose = f": {page['purpose']}" if page.get("purpose") else ""
        out.append(f"- `{page['slug']}`: {page['title']}{purpose}")
    return "\n".join(out).strip()


async def read_guide_page(ctx: Context, args: GuideArgs) -> str:
    await ctx.ready()
    page = await service.guide_page(ctx.db, ctx.objects, ctx.site.reader, ctx.build_id, args.slug)
    paths = await _paths(ctx)
    return f"# {page['title']}\n\n{_cite(page['markdown'], paths)}"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[_Args]
    schema: dict[str, Any]
    run: Callable[[Context, Any], Awaitable[str]]


_NAME = {"type": "string", "description": "the definition's name or id"}
TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "overview",
            "What the repository does, its key concepts, reading order and guide pages.",
            NoArgs,
            _schema({}, []),
            overview,
        ),
        Tool(
            "search",
            "Find definitions, files and guide pages by name or by words in their explanations.",
            SearchArgs,
            _schema(
                {
                    "query": {"type": "string", "description": "words or a name to look for"},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                },
                ["query"],
            ),
            search,
        ),
        Tool(
            "get_definition",
            "Explanation of a function, class or method: what it does, parameters, return value, notes,"
            " who uses it, what it uses, related guide pages, and its source. `name` may be 'Value.backward',"
            " 'backward' or a full id like 'micrograd/engine.py::Value.backward'.",
            DefinitionArgs,
            _schema(
                {
                    "name": _NAME,
                    "include_source": {
                        "type": "boolean",
                        "default": True,
                        "description": "include the source lines",
                    },
                },
                ["name"],
            ),
            get_definition,
        ),
        Tool(
            "find_references",
            "Every place a definition is used, with the line of code.",
            NameArgs,
            _schema({"name": _NAME}, ["name"]),
            find_references,
        ),
        Tool(
            "get_file",
            "A file's summary and an outline of its definitions with one-line explanations.",
            PathArgs,
            _schema(
                {"path": {"type": "string", "description": "file path relative to the repository root"}},
                ["path"],
            ),
            get_file,
        ),
        Tool(
            "read_lines",
            f"Source lines (at most {MAX_LINES}) with the generated notes shown under the lines they explain.",
            LinesArgs,
            _schema(
                {
                    "path": {"type": "string", "description": "file path relative to the repository root"},
                    "start": {"type": "integer", "default": 1, "minimum": 1, "description": "first line"},
                    "end": {
                        "type": "integer",
                        "minimum": 1,
                        "description": f"last line (default: start + {MAX_LINES - 1})",
                    },
                },
                ["path"],
            ),
            read_lines,
        ),
        Tool(
            "list_guide",
            "The guide pages: topic pages about architecture, flows and subsystems.",
            NoArgs,
            _schema({}, []),
            list_guide,
        ),
        Tool(
            "read_guide_page",
            "A guide page in Markdown, with citations to exact source lines as `path:start-end`.",
            GuideArgs,
            _schema(
                {"slug": {"type": "string", "description": "the page's slug, as list_guide() shows it"}},
                ["slug"],
            ),
            read_guide_page,
        ),
    )
}


def listing() -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.schema,
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        }
        for t in TOOLS.values()
    ]


class InvalidArguments(Exception):
    pass


async def call(
    db: AsyncSession, objects: ObjectStore, site: Site, name: str, arguments: dict[str, Any]
) -> tuple[str, bool]:
    """(text, is_error). Unknown tools and invalid arguments raise for a protocol error."""
    tool = TOOLS.get(name)
    if tool is None:
        raise KeyError(name)
    try:
        args = tool.args.model_validate(arguments)
    except ValidationError as error:
        raise InvalidArguments(
            "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
        ) from error
    build_id = site.build_id
    if args.language is not None:
        await set_context(db, workspace_id=site.reader.workspace_id)
        variants = await site_variants(db, site)
        match = [v for v in variants if v["language"] == args.language]
        if not match:
            return f"This site has no {args.language!r} version.", True
        build_id = UUID(str(match[0]["id"]))
    ctx = Context(db, objects, site, build_id)
    try:
        return _clip(await tool.run(ctx, args)), False
    except ToolError as error:
        return str(error), True
    except ApiError as error:
        if error.status_code in (404, 409):
            return str(error), True
        raise
