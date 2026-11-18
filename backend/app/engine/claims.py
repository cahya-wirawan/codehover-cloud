"""Claims, deterministic citation validation, the independent checker and repair (07 "Verification", 19 P05-P06).

Extraction is deterministic: each sentence or list item carrying at least one `[[file_id:start-end]]`
marker is one claim, keyed by a hash of page slug, ordinal and normalized text, paired with each cited
range. Deterministic validation runs first and needs no model: the file must be a text file of this
snapshot that passed the scan, the range must lie inside it, and the marker must be well formed. An
invalid citation is final (it can never be waived). Valid claims go to the checker, which sees only the
claim and the exact cited lines, never the writer's context. One repair cycle may rewrite or remove
unsupported or partial claims; changed claims are validated and checked again.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from app.engine import prompts
from app.engine.catalog import Model, Role
from app.engine.gateway import GenerationRequest
from app.engine.generate import CITATION, Prepared, _cache_key, _schema_errors, digest, schema

MARKER = re.compile(r"\[\[[^\[\]\n]{0,120}\]\]")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
MAX_CITED_LINES = 80
CLAIMS_PER_CALL = 25
STATUSES = ("supported", "partial", "unsupported", "unchecked", "invalid")
TEXT_STATUSES = ("selected", "unsupported")


@dataclass(frozen=True)
class Citation:
    raw: str
    file_id: str | None
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Claim:
    key: str
    ordinal: int
    text: str  # normalized, without markers
    raw: str  # exact source text, markers included (repair replaces it)
    citations: tuple[Citation, ...]


@dataclass(frozen=True)
class SourceFile:
    file_id: str
    path: str
    sha256: str
    status: str
    lines: tuple[str, ...] | None  # None when the file has no stored text


def _normalize(text: str) -> str:
    return " ".join(MARKER.sub("", text).split()).strip()


def _segments(markdown: str) -> list[str]:
    """Sentences and list items outside code fences and headings, in document order."""
    blocks: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush() -> None:
        if paragraph:
            blocks.append(" ".join(paragraph))
            paragraph.clear()

    for line in markdown.split("\n"):
        if line.lstrip().startswith("```"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            flush()
            continue
        if LIST_ITEM.match(line):
            flush()
            blocks.append(LIST_ITEM.sub("", line, count=1).strip())
            continue
        paragraph.append(stripped)
    flush()
    segments: list[str] = []
    for block in blocks:
        parts = SENTENCE_END.split(block)
        for part in parts:
            # A citation written after the full stop belongs to the sentence before it.
            leading = re.match(r"^((?:\[\[[^\]]*\]\]\s*)+)(.*)$", part)
            if leading and segments:
                segments[-1] = f"{segments[-1]} {leading.group(1).strip()}"
                part = leading.group(2)
            if part.strip():
                segments.append(part.strip())
    return segments


def extract(slug: str, markdown: str) -> list[Claim]:
    claims: list[Claim] = []
    for segment in _segments(markdown):
        markers = MARKER.findall(segment)
        if not markers:
            continue
        ordinal = len(claims) + 1
        text = _normalize(segment)
        citations = []
        for raw in markers:
            parsed = CITATION.fullmatch(raw)
            if parsed is None:
                citations.append(Citation(raw, None, 0, 0))
            else:
                citations.append(Citation(raw, parsed.group(1), int(parsed.group(2)), int(parsed.group(3))))
        key = hashlib.sha256(f"{slug}\0{ordinal}\0{text}".encode()).hexdigest()[:32]
        claims.append(Claim(key, ordinal, text, segment, tuple(citations)))
    return claims


def uncited_sentences(markdown: str) -> int:
    """Reported separately so citing fewer claims cannot look like higher faithfulness (07)."""
    return sum(1 for s in _segments(markdown) if not MARKER.search(s) and len(s.split()) >= 4)


def validate_citation(citation: Citation, files: dict[str, SourceFile]) -> str | None:
    """None when deterministically valid, else the reason code."""
    if citation.file_id is None:
        return "citation_syntax"
    source = files.get(citation.file_id)
    if source is None:
        return "file_not_in_snapshot"
    if source.status not in TEXT_STATUSES or source.lines is None:
        return "file_excluded"
    if not 1 <= citation.start_line <= citation.end_line <= len(source.lines):
        return "range_out_of_bounds"
    return None


def cited_lines(citation: Citation, files: dict[str, SourceFile]) -> dict[str, Any]:
    source = files[citation.file_id or ""]
    assert source.lines is not None
    end = min(citation.end_line, citation.start_line + MAX_CITED_LINES - 1)
    return {
        "file_id": citation.file_id,
        "path": source.path,
        "start_line": citation.start_line,
        "end_line": citation.end_line,
        "lines": prompts.numbered(list(source.lines[citation.start_line - 1 : end]), citation.start_line),
        "truncated": end < citation.end_line,
    }


# ---------------------------------------------------------------- checker (P05)
def check_requests(
    *,
    workspace_id: str,
    preset: str,
    model: Model,
    role: Role,
    prompt_version: str,
    claims: list[Claim],
    files: dict[str, SourceFile],
) -> list[tuple[Prepared, list[Claim]]]:
    """Independent checker calls over deterministically valid claims, at most 25 claims per call."""
    batches = []
    for start in range(0, len(claims), CLAIMS_PER_CALL):
        batch = claims[start : start + CLAIMS_PER_CALL]
        items = [
            {"claim_id": c.key, "claim": c.text, "cited_lines": [cited_lines(x, files) for x in c.citations]}
            for c in batch
        ]
        user = prompts.render(prompt_version, "check", CLAIMS=prompts.json_block("CLAIMS", items))
        request = GenerationRequest(
            model=model,
            role=role,
            purpose="check",
            prompt_version=prompt_version,
            system=prompts.system(prompt_version),
            user=user,
            schema=schema("claim-check"),
            evidence={"claims": items},
        )
        inputs = {
            "claims": [
                [c.key, c.text, [[x.file_id, x.start_line, x.end_line] for x in c.citations]] for c in batch
            ],
            "sources": sorted({files[x.file_id or ""].sha256 for c in batch for x in c.citations}),
            "schema": schema("claim-check")["$id"],
        }
        batches.append(
            (Prepared(request, _cache_key(workspace_id, "check", preset, prompt_version, inputs)), batch)
        )
    return batches


def validate_checks(output: dict[str, Any], batch: list[Claim]) -> list[str]:
    problems = _schema_errors("claim-check", output)
    if problems:
        return problems
    by_key = {c.key: c for c in batch}
    seen: set[str] = set()
    for check in output["checks"]:
        claim = by_key.get(check["claim_id"])
        if claim is None:
            problems.append(f"claim_id {check['claim_id']!r} is not one of the supplied claims")
            continue
        if check["claim_id"] in seen:
            problems.append(f"claim_id {check['claim_id']!r} is answered twice")
        seen.add(check["claim_id"])
        cited = {n for c in claim.citations for n in range(c.start_line, c.end_line + 1)}
        stray = [n for n in check["evidence_line_numbers"] if n not in cited]
        if stray:
            problems.append(f"{check['claim_id']}: evidence lines {stray[:5]} are not cited lines")
    return problems  # missing answers are not a problem to repair: they become unchecked


# ---------------------------------------------------------------- repair (P06)
def repair_request(
    *,
    workspace_id: str,
    preset: str,
    model: Model,
    role: Role,
    prompt_version: str,
    audience: str = prompts.DEFAULT_VARIANT,
    flagged: list[tuple[Claim, str, str]],
    files: dict[str, SourceFile],
) -> Prepared:
    """flagged: (claim, status, checker reason) for unsupported and partial claims of one page."""
    items = [
        {
            "claim_id": claim.key,
            "claim": claim.text,
            "status": status,
            "checker_feedback": reason,
            "cited_lines": [cited_lines(x, files) for x in claim.citations],
        }
        for claim, status, reason in flagged
    ]
    user = prompts.render(prompt_version, "claim_repair", CLAIMS=prompts.json_block("CLAIMS", items))
    request = GenerationRequest(
        model=model,
        role=role,
        purpose="claim_repair",
        prompt_version=prompt_version,
        system=prompts.system(prompt_version, audience),
        user=user,
        schema=schema("claim-repair"),
        evidence={"claims": items},
    )
    inputs = {"items": digest(items), "schema": schema("claim-repair")["$id"]}
    return Prepared(
        request, _cache_key(workspace_id, "claim_repair", preset, prompt_version, inputs, audience)
    )


def validate_repair(output: dict[str, Any], flagged: list[Claim]) -> list[str]:
    problems = _schema_errors("claim-repair", output)
    if problems:
        return problems
    by_key = {c.key: c for c in flagged}
    seen: set[str] = set()
    for item in output["replacements"]:
        claim = by_key.get(item["claim_id"])
        if claim is None or item["claim_id"] in seen:
            problems.append(f"claim_id {item['claim_id']!r} is not a flagged claim or appears twice")
            continue
        seen.add(item["claim_id"])
        if item["action"] == "remove":
            continue
        if not item["text"].strip() or MARKER.search(item["text"]) or "\n" in item["text"]:
            problems.append(f"{item['claim_id']}: text must be one sentence without citation markers")
        if not item["citations"]:
            problems.append(f"{item['claim_id']}: a replacement keeps at least one citation")
        allowed = [(c.file_id, c.start_line, c.end_line) for c in claim.citations if c.file_id]
        for cite in item["citations"]:
            if not any(
                f == cite["file_id"] and s <= cite["start_line"] <= cite["end_line"] <= e
                for f, s, e in allowed
            ):
                problems.append(f"{item['claim_id']}: citation outside the claim's cited lines")
    return problems


def apply_repair(markdown: str, claims: list[Claim], output: dict[str, Any]) -> str:
    by_key = {c.key: c for c in claims}
    for item in output["replacements"]:
        claim = by_key.get(item["claim_id"])
        if claim is None or claim.raw not in markdown:
            continue
        if item["action"] == "remove":
            replacement = ""
        else:
            markers = " ".join(
                f"[[{c['file_id']}:{c['start_line']}-{c['end_line']}]]" for c in item["citations"]
            )
            text = item["text"].strip().rstrip(".")
            replacement = f"{text} {markers}."
        markdown = markdown.replace(claim.raw, replacement, 1)
    return re.sub(r"\n\s*[-*+]\s*\n", "\n", markdown)  # drop list items emptied by removal


# ---------------------------------------------------------------- quality
def page_status(statuses: list[str]) -> str:
    """Worst current status of a page's claims, for its badge."""
    for status in ("invalid", "unsupported", "unchecked", "partial"):
        if status in statuses:
            return status
    return "supported" if statuses else "unchecked"


def publication_blockers(
    counts: dict[str, int], waived_unsupported: int, pages_failed: int, pages_uncited: int = 0
) -> list[str]:
    """Default gate (07): all checks executed, zero invalid citations, no unwaived unsupported claims.
    Partial claims do not block but need an admin acknowledgment at publication (M3)."""
    blockers = []
    if counts.get("invalid", 0):
        blockers.append("invalid_citations")
    if counts.get("unchecked", 0):
        blockers.append("unchecked_claims")
    if counts.get("unsupported", 0) > waived_unsupported:
        blockers.append("unsupported_claims")
    if pages_failed:
        blockers.append("failed_pages")
    if pages_uncited:  # a written page with no checked claim (for example all removed by repair)
        blockers.append("uncited_pages")
    return blockers
