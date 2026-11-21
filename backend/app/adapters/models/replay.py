"""Deterministic replay gateway (07 "Start with one approved provider and a deterministic replay adapter").

It answers every pipeline purpose from the structured evidence the prompt was rendered from, never from
the prompt text, so repository content cannot steer it. Answers satisfy the same schemas and local
validation as provider output. Usage is reported from request size, and the replay catalog model costs
nothing. Replay preset builds exercise the full pipeline (budgets, attempts, cache, guide) for free.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import PurePosixPath
from typing import Any

from app.engine.gateway import GenerationRequest, GenerationResult, Usage

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _first_sentence(text: str, limit: int) -> str:
    sentence = text.strip().split("\n\n")[0].replace("\n", " ")
    end = sentence.find(". ")
    return (sentence if end == -1 else sentence[: end + 1])[:limit]


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    return slug or "code"


class ReplayGateway:
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        evidence = request.evidence
        purpose = evidence.get("repair_of", request.purpose)
        output = getattr(self, f"_{purpose}")(evidence)
        usage = Usage(input_tokens=len(request.user.encode()) // 4, output_tokens=len(str(output)) // 4)
        return GenerationResult(
            output=output, usage=usage, provider_request_id=None, model_id=request.model.id
        )

    # P02
    def _explain(self, evidence: dict[str, Any]) -> dict[str, Any]:
        cards = []
        for symbol in evidence["symbols"]:
            doc = symbol.get("docstring")
            summary = (
                _first_sentence(doc, 600)
                if doc
                else f"{symbol['kind'].capitalize()} `{symbol['name']}` in `{evidence['path']}`."
            )
            cards.append(
                {
                    "symbol_key": symbol["symbol_key"],
                    "summary": summary,
                    "details": f"Signature: `{symbol['signature']}`"[:3000]
                    if symbol.get("signature")
                    else "",
                    "params": [
                        {"name": p, "description": f"Parameter `{p}`."} for p in symbol["params"][:100]
                    ],
                    "returns": "",
                    "notes": "Replay generation: derived from the source only, no model was called.",
                }
            )
        names = ", ".join(f"`{s['name']}`" for s in evidence["symbols"][:8])
        summary = f"Lines {evidence['start_line']}-{evidence['end_line']} of `{evidence['path']}`" + (
            f" define {names}." if names else "."
        )
        return {"summary": summary[:2000], "symbols": cards, "notes": []}

    # P03
    def _overview(self, evidence: dict[str, Any]) -> dict[str, Any]:
        summaries = evidence["summaries"]
        directories = OrderedDict[str, int]()
        for item in summaries:
            top = PurePosixPath(item["path"]).parts[0] if "/" in item["path"] else "(root)"
            directories[top] = directories.get(top, 0) + 1
        overview = (
            f"The selected scope has {len(summaries)} file(s) in {len(directories)} top-level area(s): "
            + ", ".join(f"`{name}` ({count})" for name, count in directories.items())
        )
        return {
            "overview": overview[:3000],
            "key_concepts": [
                {"term": name, "explanation": f"Top-level area with {count} selected file(s)."}
                for name, count in list(directories.items())[:10]
            ],
            "reading_order": [
                {"file_id": item["file_id"], "reason": item["summary"][:500] or "Selected file."}
                for item in summaries[:12]
            ],
        }

    # P01
    def _plan(self, evidence: dict[str, Any]) -> dict[str, Any]:
        files = evidence["files"]
        minimum, maximum = evidence["bounds"]
        groups = OrderedDict[str, list[dict[str, Any]]]()
        for f in files:
            key = str(PurePosixPath(f["path"]).parent) if "/" in f["path"] else f["path"]
            groups.setdefault(key, []).append(f)
        buckets = list(groups.items())
        while len(buckets) < minimum and any(len(b[1]) > 1 for b in buckets):  # split to reach the minimum
            index = max(range(len(buckets)), key=lambda i: len(buckets[i][1]))
            name, items = buckets[index]
            buckets[index : index + 1] = [(f"{name}-{i + 1}", [item]) for i, item in enumerate(items)]
        pages, used = [], set()
        for name, items in buckets[:maximum]:
            slug = _slug(name)
            while slug in used:
                slug = f"{slug}-x"[:64]
            used.add(slug)
            pages.append(
                {
                    "slug": slug,
                    "title": f"How `{name}` works"[:200],
                    "section": "Code walkthrough",
                    "purpose": f"Walk through the definitions in `{name}`."[:1000],
                    "source_ranges": [
                        {
                            "file_id": f["file_id"],
                            "path": f["path"],
                            "start_line": 1,
                            "end_line": max(1, min(f["lines"], 120)),
                        }
                        for f in items[:6]
                    ],
                }
            )
        return {"pages": pages, "reason": ""}

    # P04
    def _guide(self, evidence: dict[str, Any]) -> dict[str, Any]:
        page = evidence["page"]
        parts = [f"# {page['title']}", "", page["purpose"], ""]
        for e in evidence["evidence"]:
            defined = [
                (e["start_line"] + i, m.group(1))
                for i, line in enumerate(e["lines"])
                if (
                    m := re.match(
                        r"\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|interface|type)\s+([A-Za-z_]\w*)",
                        line,
                    )
                )
            ]
            if not defined:
                parts.append(
                    f"- `{e['path']}` contributes lines {e['start_line']}-{e['end_line']} [[{e['file_id']}:{e['start_line']}-{e['end_line']}]]."
                )
            for line, name in defined[:12]:
                parts.append(f"- `{e['path']}` defines `{name}` [[{e['file_id']}:{line}-{line}]].")
        parts += ["", "Behavior beyond these definitions is not described by this replay page."]
        return {"markdown": "\n".join(parts)[:20000]}

    # P05: supported when every code identifier the claim names appears in its cited lines.
    def _check(self, evidence: dict[str, Any]) -> dict[str, Any]:
        checks = []
        for item in evidence["claims"]:
            names = [
                t
                for t in re.findall(r"`([^`]+)`", item["claim"])
                if "/" not in t and not re.search(r"\.\w+$", t)
            ]
            numbered = [
                (int(row.split("|", 1)[0]), row.split("|", 1)[1] if "|" in row else "")
                for c in item["cited_lines"]
                for row in c["lines"].split("\n")
                if row.split("|", 1)[0].strip().isdigit()
            ]
            found = [n for n in names if any(re.search(rf"\b{re.escape(n)}\b", text) for _, text in numbered)]
            lines = sorted(
                {
                    line
                    for line, text in numbered
                    if any(re.search(rf"\b{re.escape(n)}\b", text) for n in found)
                }
            )
            if names and len(found) == len(names):
                status, reason = "supported", "Every named identifier appears in the cited lines."
            elif found:
                status, reason = "partial", "Only some named identifiers appear in the cited lines."
            elif names:
                status, reason = "unsupported", "None of the named identifiers appear in the cited lines."
            else:
                status, reason = "partial", "The claim names nothing the replay checker can match."
            checks.append(
                {
                    "claim_id": item["claim_id"],
                    "status": status,
                    "reason": reason,
                    "evidence_line_numbers": lines,
                }
            )
        return {"checks": checks}

    # P06: the replay repair never invents text; it removes what the checker could not support.
    def _claim_repair(self, evidence: dict[str, Any]) -> dict[str, Any]:
        return {
            "replacements": [
                {"claim_id": item["claim_id"], "text": "", "citations": [], "action": "remove"}
                for item in evidence["claims"]
                if item["status"] == "unsupported"
            ]
        }
