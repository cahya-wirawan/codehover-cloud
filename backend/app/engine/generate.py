"""Pure generation logic: chunking, request construction and output validation (07, 19 P01-P04).

Nothing here does I/O. Stage handlers load the evidence, call `*_request` to build a GenerationRequest
(rendered prompt plus the structured evidence it came from), and validate the answer with the matching
`validate_*`, which returns problems for one repair call. Local validation enforces every limit the
provider schema projection drops.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from markdown_it import MarkdownIt
from markdown_it.token import Token

from app.engine import ENGINE_VERSION, prompts
from app.engine.catalog import Model, Role
from app.engine.gateway import GenerationRequest

SCHEMAS = Path(__file__).with_name("schemas")
EXPLANATION_SCHEMA_ID = "urn:codehover-cloud:schema:explanation:2.0.0"
CHUNK_TARGET_LINES = 450
CHUNK_MAX_LINES = 500
REPO_MAP_MAX_FILES = 200
REPO_MAP_MAX_CHARS = 8000
README_MAX_CHARS = 4000
SUMMARY_MAX_CHARS = 600
GUIDE_EVIDENCE_MAX_LINES = 600
MIN_PAGES, MAX_PAGES = 3, 8
MAX_RANGE_LINES = 200
CITATION = re.compile(r"\[\[([0-9a-f-]{36}):(\d+)-(\d+)\]\]")
SINGLE_LINE_CITATION = re.compile(r"\[\[([0-9a-f-]{36}):(\d+)\]\]")
MERMAID_FORBIDDEN = re.compile(r"(?i)\b(click|style|classDef|linkStyle|%%\{|init)\b|<[a-z/]")
MERMAID_NODE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\s*[\[({>]")
SCHEMA_MAJOR_MINOR = ENGINE_VERSION.rsplit(".", 1)[0]


@lru_cache(maxsize=8)
def schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    return loaded


def _schema_errors(name: str, output: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema(name))
    return [
        f"{'/'.join(str(p) for p in error.path) or '(root)'}: {error.message}"[:300]
        for error in sorted(validator.iter_errors(output), key=lambda e: list(e.path))[:20]
    ]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------- evidence
@dataclass(frozen=True)
class SymbolInfo:
    symbol_key: str
    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    params: tuple[str, ...]
    docstring: str | None
    parent_key: str | None

    def evidence(self) -> dict[str, Any]:
        return {
            "symbol_key": self.symbol_key,
            "name": self.name,
            "kind": self.kind,
            "lines": [self.start_line, self.end_line],
            "signature": self.signature,
            "params": list(self.params),
        }


@dataclass(frozen=True)
class FileInfo:
    file_id: str
    path: str
    language: str
    sha256: str
    lines: tuple[str, ...]
    symbols: tuple[SymbolInfo, ...]


@dataclass(frozen=True)
class Chunk:
    start_line: int
    end_line: int
    symbols: tuple[SymbolInfo, ...]
    incomplete: bool = False  # a definition longer than the chunk limit was cut into subranges


def chunks(file: FileInfo) -> list[Chunk]:
    """Break at top-level definitions, targeting 400-500 lines; a longer definition is split into bounded
    subranges flagged incomplete, never silently truncated (07)."""
    total = max(1, len(file.lines))
    tops = sorted({s.start_line for s in file.symbols if s.parent_key is None and 1 < s.start_line <= total})
    spans: list[tuple[int, int, bool]] = []

    def emit(begin: int, end: int) -> None:
        split = end - begin + 1 > CHUNK_MAX_LINES  # one definition longer than a chunk
        while end - begin + 1 > CHUNK_MAX_LINES:
            spans.append((begin, begin + CHUNK_MAX_LINES - 1, True))
            begin += CHUNK_MAX_LINES
        if end >= begin:
            spans.append((begin, end, split))

    start, previous = 1, 1
    for cut in [*tops, total + 1]:
        if cut - start > CHUNK_MAX_LINES and previous > start:  # close before the definition that overflows
            emit(start, previous - 1)
            start = previous
        if cut - start >= CHUNK_TARGET_LINES or cut == total + 1:
            emit(start, cut - 1)
            start = cut
        previous = cut
    result = []
    for begin, end, cut_definition in spans:
        inside = tuple(s for s in file.symbols if begin <= s.start_line <= end)
        spilled = any(s.end_line > end for s in inside)
        result.append(Chunk(begin, end, inside, incomplete=cut_definition or spilled))
    return result


def repo_map(files: list[FileInfo]) -> list[dict[str, Any]]:
    """Bounded map of the selected scope: paths and top-level names only, never excluded files."""
    entries, size = [], 0
    for f in files[:REPO_MAP_MAX_FILES]:
        names = [s.name for s in f.symbols if s.parent_key is None][:12]
        entry = {"file_id": f.file_id, "path": f.path, "lines": len(f.lines), "defines": names}
        size += len(json.dumps(entry))
        if size > REPO_MAP_MAX_CHARS:
            break
        entries.append(entry)
    return entries


def _cache_key(
    workspace_id: str,
    purpose: str,
    preset: str,
    prompt_version: str,
    inputs: Any,
    audience: str = prompts.DEFAULT_VARIANT,
) -> str:
    return digest(
        {
            "workspace": workspace_id,
            "purpose": purpose,
            "engine": SCHEMA_MAJOR_MINOR,
            "prompts": prompt_version.rsplit(".", 1)[0],
            "templates": prompts.digest(prompt_version),
            "preset": preset,
            "variant": audience,
            "inputs": inputs,
        }
    )


@dataclass(frozen=True)
class Prepared:
    """A request plus the cache key of its validated answer."""

    request: GenerationRequest
    cache_key: str
    context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- explain (P02)
def explain_request(
    *,
    workspace_id: str,
    preset: str,
    model: Model,
    role: Role,
    prompt_version: str,
    audience: str = prompts.DEFAULT_VARIANT,
    file: FileInfo,
    chunk: Chunk,
    related: list[dict[str, Any]],
    map_entries: list[dict[str, Any]],
) -> Prepared:
    source = prompts.numbered(list(file.lines[chunk.start_line - 1 : chunk.end_line]), chunk.start_line)
    symbols = [s.evidence() for s in chunk.symbols]
    incomplete = (
        "- This chunk does not contain the whole of every definition; say so where a summary depends on lines outside it."
        if chunk.incomplete
        else ""
    )
    user = prompts.render(
        prompt_version,
        "explain",
        PATH=file.path,
        START_LINE=str(chunk.start_line),
        END_LINE=str(chunk.end_line),
        INCOMPLETE=incomplete,
        SOURCE_CHUNK=prompts.data_block("SOURCE_CHUNK", f"path: {file.path}\n{source}"),
        SYMBOLS=prompts.json_block("SYMBOLS", symbols),
        RELATED_CONTEXT=prompts.json_block("RELATED_CONTEXT", related),
        REPO_MAP=prompts.json_block("REPO_MAP", map_entries),
    )
    evidence = {
        "path": file.path,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "symbols": [{**s.evidence(), "docstring": s.docstring} for s in chunk.symbols],
    }
    inputs = {
        "file": file.sha256,
        "range": [chunk.start_line, chunk.end_line],
        "symbols": digest(symbols),
        "related": digest(related),
        "map": digest(map_entries),
        "schema": EXPLANATION_SCHEMA_ID,
    }
    request = GenerationRequest(
        model=model,
        role=role,
        purpose="explain",
        prompt_version=prompt_version,
        system=prompts.system(prompt_version, audience),
        user=user,
        schema=schema("explanation"),
        evidence=evidence,
    )
    return Prepared(request, _cache_key(workspace_id, "explain", preset, prompt_version, inputs, audience))


def validate_explanation(output: dict[str, Any], chunk: Chunk) -> list[str]:
    problems = _schema_errors("explanation", output)
    if problems:
        return problems
    expected = {s.symbol_key: s for s in chunk.symbols}
    seen: set[str] = set()
    for card in output["symbols"]:
        key = card["symbol_key"]
        if key not in expected:
            problems.append(f"symbol_key {key!r} is not one of the supplied SYMBOLS")
            continue
        if key in seen:
            problems.append(f"symbol_key {key!r} appears twice")
        seen.add(key)
        allowed = set(expected[key].params)
        for param in card["params"]:
            if param["name"] not in allowed:
                problems.append(f"{key}: parameter {param['name']!r} is not a parameter of this symbol")
    missing = sorted(set(expected) - seen)
    if missing:
        problems.append(f"missing symbols: {', '.join(missing[:10])}")
    for note in output["notes"]:
        if not chunk.start_line <= note["start_line"] <= note["end_line"] <= chunk.end_line:
            problems.append(
                f"note range {note['start_line']}-{note['end_line']} is outside lines {chunk.start_line}-{chunk.end_line}"
            )
    return problems


# ---------------------------------------------------------------- overview (P03)
def overview_request(
    *,
    workspace_id: str,
    preset: str,
    model: Model,
    role: Role,
    prompt_version: str,
    audience: str = prompts.DEFAULT_VARIANT,
    summaries: list[dict[str, Any]],
    map_entries: list[dict[str, Any]],
    readme: str,
) -> Prepared:
    readme = readme[:README_MAX_CHARS]
    user = prompts.render(
        prompt_version,
        "overview",
        FILE_SUMMARIES=prompts.json_block("FILE_SUMMARIES", summaries),
        REPO_MAP=prompts.json_block("REPO_MAP", map_entries),
        SAFE_README=prompts.data_block("SAFE_README", readme or "(no README in scope)"),
    )
    request = GenerationRequest(
        model=model,
        role=role,
        purpose="overview",
        prompt_version=prompt_version,
        system=prompts.system(prompt_version, audience),
        user=user,
        schema=schema("overview"),
        evidence={"summaries": summaries},
    )
    inputs = {"summaries": digest(summaries), "map": digest(map_entries), "readme": digest(readme)}
    return Prepared(request, _cache_key(workspace_id, "overview", preset, prompt_version, inputs, audience))


def validate_overview(output: dict[str, Any], file_ids: set[str]) -> list[str]:
    problems = _schema_errors("overview", output)
    if problems:
        return problems
    for item in output["reading_order"]:
        if item["file_id"] not in file_ids:
            problems.append(f"reading_order names {item['file_id']!r}, which is not in FILE_SUMMARIES")
    return problems


# ---------------------------------------------------------------- plan (P01)
def page_bounds(selected_files: int, requested: int) -> tuple[int, int]:
    """3-8 pages, at most the configured count; tiny scopes may have 1-2 pages instead of filler (19 P01)."""
    maximum = min(MAX_PAGES, requested)
    minimum = min(MIN_PAGES, maximum, max(1, selected_files))
    return minimum, maximum


def plan_request(
    *,
    workspace_id: str,
    preset: str,
    model: Model,
    role: Role,
    prompt_version: str,
    audience: str = prompts.DEFAULT_VARIANT,
    commit_sha: str,
    summaries: list[dict[str, Any]],
    map_entries: list[dict[str, Any]],
    readme: str,
    bounds: tuple[int, int],
) -> Prepared:
    readme = readme[:README_MAX_CHARS]
    user = prompts.render(
        prompt_version,
        "plan",
        MIN_PAGES=str(bounds[0]),
        MAX_PAGES=str(bounds[1]),
        SNAPSHOT_SHA=commit_sha,
        FILE_SUMMARIES=prompts.json_block("FILE_SUMMARIES", summaries),
        REPO_MAP=prompts.json_block("REPO_MAP", map_entries),
        SAFE_README=prompts.data_block("SAFE_README", readme or "(no README in scope)"),
    )
    request = GenerationRequest(
        model=model,
        role=role,
        purpose="plan",
        prompt_version=prompt_version,
        system=prompts.system(prompt_version, audience),
        user=user,
        schema=schema("guide-plan"),
        evidence={"files": map_entries, "bounds": list(bounds)},
    )
    inputs = {
        "summaries": digest(summaries),
        "map": digest(map_entries),
        "readme": digest(readme),
        "bounds": bounds,
    }
    return Prepared(request, _cache_key(workspace_id, "plan", preset, prompt_version, inputs, audience))


def validate_plan(
    output: dict[str, Any], files: dict[str, tuple[str, int]], bounds: tuple[int, int]
) -> list[str]:
    """files: file_id -> (path, line count) of the selected scope."""
    problems = _schema_errors("guide-plan", output)
    if problems:
        return problems
    pages = output["pages"]
    if not bounds[0] <= len(pages) <= bounds[1]:
        problems.append(f"return between {bounds[0]} and {bounds[1]} pages, not {len(pages)}")
    slugs = [p["slug"] for p in pages]
    if len(set(slugs)) != len(slugs):
        problems.append("slugs must be unique")
    for page in pages:
        if not page["source_ranges"]:
            problems.append(f"page {page['slug']!r} has no source ranges")
        for r in page["source_ranges"]:
            known = files.get(r["file_id"])
            if known is None or known[0] != r["path"]:
                problems.append(f"page {page['slug']!r}: {r['file_id']}/{r['path']} is not a supplied file")
                continue
            if not 1 <= r["start_line"] <= r["end_line"] <= known[1]:
                problems.append(
                    f"page {page['slug']!r}: lines {r['start_line']}-{r['end_line']} are outside {r['path']}"
                )
            elif r["end_line"] - r["start_line"] + 1 > MAX_RANGE_LINES:
                problems.append(f"page {page['slug']!r}: a range may cover at most {MAX_RANGE_LINES} lines")
    return problems


# ---------------------------------------------------------------- guide page (P04)
def guide_request(
    *,
    workspace_id: str,
    preset: str,
    model: Model,
    role: Role,
    prompt_version: str,
    audience: str = prompts.DEFAULT_VARIANT,
    page: dict[str, Any],
    evidence: list[dict[str, Any]],
    guide_plan: list[dict[str, Any]] | None = None,
    definitions: list[dict[str, Any]] | None = None,
) -> Prepared:
    """evidence: [{file_id, path, start_line, end_line, sha256, lines}] in plan order, already bounded.
    guide_plan: every page of the guide ({slug, title, purpose}), so a page can link the others;
    definitions: the definitions inside the evidence ({name, kind, path, lines, summary}), so the page
    names them exactly. Sets from 0.5.0 read both; older sets ignore them."""
    blocks = "\n\n".join(
        f"file_id: {e['file_id']}\npath: {e['path']}\n{prompts.numbered(e['lines'], e['start_line'])}"
        for e in evidence
    )
    plan = {"slug": page["slug"], "title": page["title"], "purpose": page["purpose"]}
    toc = [{k: p[k] for k in ("slug", "title", "purpose")} for p in guide_plan or []]
    names = (definitions or [])[:MAX_GUIDE_DEFINITIONS]
    user = prompts.render(
        prompt_version,
        "guide",
        PAGE_PLAN=prompts.json_block("PAGE_PLAN", plan),
        GUIDE_PLAN=prompts.json_block("GUIDE_PLAN", toc),
        DEFINITIONS=prompts.json_block("DEFINITIONS", names),
        SOURCE_EVIDENCE=prompts.data_block("SOURCE_EVIDENCE", blocks),
    )
    request = GenerationRequest(
        model=model,
        role=role,
        purpose="guide",
        prompt_version=prompt_version,
        system=prompts.system(prompt_version, audience),
        user=user,
        schema=schema("guide-page"),
        evidence={"page": plan, "evidence": evidence},
    )
    inputs: dict[str, Any] = {
        "page": plan,
        "evidence": [[e["file_id"], e["sha256"], e["start_line"], e["end_line"]] for e in evidence],
        "schema": schema("guide-page")["$id"],
    }
    if prompts.uses(prompt_version, "guide", "GUIDE_PLAN"):
        inputs["guide"] = toc
    if prompts.uses(prompt_version, "guide", "DEFINITIONS"):
        inputs["definitions"] = names
    return Prepared(request, _cache_key(workspace_id, "guide", preset, prompt_version, inputs, audience))


MAX_GUIDE_DEFINITIONS = 60


def evidence_definitions(evidence: list[dict[str, Any]], symbols: list[Any]) -> list[dict[str, Any]]:
    """The definitions that start inside a page's evidence ranges, in source order: what the page can name
    in backticks. `symbols` rows carry source_file_id, name, kind, start_line, end_line and summary."""
    ranges = [(e["file_id"], e["start_line"], e["end_line"], e["path"]) for e in evidence]
    found = []
    for s in sorted(symbols, key=lambda s: (str(s.source_file_id), s.start_line)):
        where = next(
            (r for r in ranges if r[0] == str(s.source_file_id) and r[1] <= s.start_line <= r[2]), None
        )
        if where:
            found.append(
                {
                    "name": s.name,
                    "kind": s.kind,
                    "path": where[3],
                    "lines": f"{s.start_line}-{s.end_line}",
                    "summary": (s.summary or "")[:200],
                }
            )
    return found[:MAX_GUIDE_DEFINITIONS]


def bounded_evidence(
    ranges: list[dict[str, Any]], texts: dict[str, list[str]], shas: dict[str, str]
) -> list[dict[str, Any]]:
    """Cut planned ranges to the evidence budget, in order (07: no silent truncation: the cut is visible
    to the writer because only the included lines are supplied)."""
    remaining, result = GUIDE_EVIDENCE_MAX_LINES, []
    for r in ranges:
        if remaining <= 0:
            break
        lines = texts[r["file_id"]]
        end = min(r["end_line"], len(lines), r["start_line"] + remaining - 1)
        if end < r["start_line"]:
            continue
        result.append(
            {
                "file_id": r["file_id"],
                "path": r["path"],
                "start_line": r["start_line"],
                "end_line": end,
                "sha256": shas[r["file_id"]],
                "lines": lines[r["start_line"] - 1 : end],
            }
        )
        remaining -= end - r["start_line"] + 1
    return result


def _markdown_parser() -> MarkdownIt:
    """CommonMark plus tables (the viewer parses the same way), with HTML recognized (so it can be
    rejected) and link validation off (so every link target is visible to the allowlist instead of being
    dropped by this parser but not by another)."""
    parser = MarkdownIt("commonmark", {"html": True}).enable("table")
    parser.validateLink = lambda url: True  # type: ignore[method-assign]
    return parser


GUIDE_LINK = "guide:"  # a link to another page of the same guide: [title](guide:slug)


def markdown_problems(markdown: str, pages: frozenset[str] = frozenset()) -> list[str]:
    """Allowlist over CommonMark tokens (19 P04): no raw HTML, no images, links only to https URLs,
    in-page anchors or `guide:<slug>` for a page in `pages`, bounded strict Mermaid. Checking tokens
    rather than regexes avoids parser differentials between this check and a renderer."""
    problems: list[str] = []
    tokens = _markdown_parser().parse(markdown)
    if not tokens or tokens[0].type != "heading_open" or tokens[0].tag != "h1":
        problems.append("start with a level-one heading")

    def walk(items: list[Token]) -> None:
        for token in items:
            if token.type in ("html_block", "html_inline"):
                problems.append("raw HTML is not allowed")
            elif token.type == "image":
                problems.append("images are not allowed")
            elif token.type == "link_open":
                href = str(token.attrGet("href") or "")
                if href.startswith(GUIDE_LINK):
                    if href[len(GUIDE_LINK) :] not in pages:
                        problems.append(f"link target {href[:80]!r} is not a page of this guide")
                elif not (href.startswith("https://") or href.startswith("#")):
                    problems.append(f"link target {href[:80]!r} is not an https URL")
            elif token.type == "fence" and token.info.strip().lower().startswith("mermaid"):
                if MERMAID_FORBIDDEN.search(token.content):
                    problems.append("mermaid diagrams may not use click, style, init or HTML")
                if len(set(MERMAID_NODE.findall(token.content))) > 30:
                    problems.append("mermaid diagrams may have at most 30 nodes")
            if token.children:
                walk(token.children)

    walk(tokens)
    return sorted(set(problems), key=problems.index)


def heading_text(markdown: str) -> str | None:
    """The plain text of a page's leading level-one heading (its title)."""
    tokens = _markdown_parser().parse(markdown)
    if len(tokens) >= 2 and tokens[0].type == "heading_open" and tokens[0].tag == "h1" and tokens[1].children:
        text = "".join(t.content for t in tokens[1].children if t.type in ("text", "code_inline")).strip()
        return text or None
    return None


def normalize_citations(markdown: str) -> str:
    """Writes a single-line citation `[[file_id:N]]`, which models write despite the prompt, as the
    canonical `[[file_id:N-N]]`. Applied once to a written page, before it is checked and stored, so
    claims, the viewer and exports only ever see the canonical form."""
    return SINGLE_LINE_CITATION.sub(r"[[\1:\2-\2]]", markdown)


def validate_guide(
    output: dict[str, Any], evidence: list[dict[str, Any]], pages: frozenset[str] = frozenset()
) -> list[str]:
    """Allowlisted Markdown (19 P04), checked on the page as it will be stored (`normalize_citations`).
    Citation correctness against the snapshot is the verify stage's job; here every citation marker must
    be well formed and point into the supplied evidence, so a malformed one is repaired, not stored."""
    from app.engine.claims import markers  # claims imports this module

    problems = _schema_errors("guide-page", output)
    if problems:
        return problems
    markdown = normalize_citations(output["markdown"])
    problems.extend(markdown_problems(markdown, pages))
    for raw in markers(markdown):
        if CITATION.fullmatch(raw) is None:
            problems.append(
                f"citation {raw[:80]!r} is not [[file_id:start-end]] with a file_id from SOURCE_EVIDENCE"
            )
    ranges = [(e["file_id"], e["start_line"], e["end_line"]) for e in evidence]
    citations = CITATION.findall(markdown)
    if not citations:
        problems.append("cite the source evidence with [[file_id:start-end]]")
    for file_id, start, end in citations:
        a, b = int(start), int(end)
        if not any(f == file_id and s <= a <= b <= e for f, s, e in ranges):
            problems.append(f"citation [[{file_id}:{start}-{end}]] is not inside SOURCE_EVIDENCE")
    return problems[:20]


# ---------------------------------------------------------------- translation (P07)
LINK_TARGET = re.compile(r"\]\((guide:[a-z0-9-]+|https://[^)\s]+)\)")
TRANSLATION_SCHEMAS = {"explanation": "explanation", "overview": "overview", "page": "guide-page"}
TRANSLATION_FORMATS = {
    "explanation": "Explanation JSON",
    "overview": "Overview JSON",
    "page": "guide page JSON whose `markdown` is the translated page",
}


def translate_request(
    *,
    workspace_id: str,
    preset: str,
    model: Model,
    role: Role,
    prompt_version: str,
    audience: str,
    kind: str,
    source: dict[str, Any],
    source_language: str,
) -> Prepared:
    """A language variant's unit translated from its base build's validated output (P07, prompt sets from
    0.6.0): `kind` is explanation (one chunk's Explanation JSON), overview, or page ({markdown}). The
    audience instruction names the target language; the answer has the source's schema."""
    name = TRANSLATION_SCHEMAS[kind]
    user = prompts.render(
        prompt_version,
        "translate",
        SOURCE_LANGUAGE=prompts.LANGUAGES[source_language],
        FORMAT=TRANSLATION_FORMATS[kind],
        SOURCE_TEXT=prompts.json_block("SOURCE_TEXT", source),
    )
    request = GenerationRequest(
        model=model,
        role=role,
        purpose="translate",
        prompt_version=prompt_version,
        system=prompts.system(prompt_version, audience),
        user=user,
        schema=schema(name),
        evidence={"kind": kind, "source": source},
    )
    inputs = {"kind": kind, "source": digest(source), "from": source_language, "schema": schema(name)["$id"]}
    return Prepared(request, _cache_key(workspace_id, "translate", preset, prompt_version, inputs, audience))


def translation_problems(kind: str, output: dict[str, Any], source: dict[str, Any]) -> list[str]:
    """What a translation changed besides prose: structure, identifiers, ranges and citation markers
    must equal the source's exactly (19 P07). Run after the unit's own validation."""
    problems: list[str] = []
    if kind == "explanation":
        theirs = {c["symbol_key"]: [p["name"] for p in c["params"]] for c in source["symbols"]}
        ours = {c["symbol_key"]: [p["name"] for p in c["params"]] for c in output.get("symbols", [])}
        if ours != theirs:
            problems.append("keep every symbol_key and parameter name of SOURCE_TEXT, in the same order")
        ranges = [(n["start_line"], n["end_line"]) for n in source["notes"]]
        if [(n["start_line"], n["end_line"]) for n in output.get("notes", [])] != ranges:
            problems.append("keep the same notes with the same line ranges, in the same order")
    elif kind == "overview":
        if [i["file_id"] for i in output.get("reading_order", [])] != [
            i["file_id"] for i in source["reading_order"]
        ]:
            problems.append("keep the reading_order's file_ids in the same order")
        if len(output.get("key_concepts", [])) != len(source["key_concepts"]):
            problems.append("keep the same number of key_concepts")
    else:
        markdown = normalize_citations(str(output.get("markdown", "")))
        if sorted(CITATION.findall(markdown)) != sorted(CITATION.findall(source["markdown"])):
            problems.append("keep every citation marker of SOURCE_TEXT exactly once, unchanged")
        # In-page anchors follow the translated headings; links to pages and sites stay as they are.
        if sorted(LINK_TARGET.findall(markdown)) != sorted(LINK_TARGET.findall(source["markdown"])):
            problems.append("keep every guide: and https link target of SOURCE_TEXT unchanged")
    return problems


# ---------------------------------------------------------------- repair
def repair_request(prepared: Prepared, previous: dict[str, Any], problems: list[str]) -> GenerationRequest:
    original = prepared.request
    user = prompts.render(
        original.prompt_version,
        "repair",
        PROBLEMS=prompts.json_block("PROBLEMS", problems[:20]),
        PREVIOUS_ANSWER=prompts.json_block("PREVIOUS_ANSWER", previous),
        ORIGINAL_TASK=original.user,
    )
    return GenerationRequest(
        model=original.model,
        role=original.role,
        purpose="repair",
        prompt_version=original.prompt_version,
        system=original.system,
        user=user,
        schema=original.schema,
        evidence={**original.evidence, "repair_of": original.purpose, "problems": problems},
    )
