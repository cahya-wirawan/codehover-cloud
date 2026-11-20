"""Labeled evaluation harness (07 "Evaluation corpus and release gates", 13 "AI eval").

Runs the engine on the redistributable corpus in `evals/corpus` and scores it against `evals/labels`:

- scan: hard exclusions, secret blocking and excluded directories behave as labeled;
- definitions: parser spans and kinds against labeled definitions (precision/recall), plus span
  validity of every parsed symbol (must be 100%);
- explanations: every chunk answer passes local validation; injected text in the corpus never
  appears in an answer; every prompt keeps its data blocks intact;
- citations: labeled invalid citations are rejected with the labeled reason (must be 100%);
- checker: labels against each labeler, supported-label precision and recall with Wilson 95%
  intervals, a confusion matrix, and Cohen's kappa between labelers when two or more exist;
- cost: request bytes and reported usage, so prompt changes show token regressions.

The default gateway is the deterministic replay, so the report is reproducible and free; CI runs it.
`--provider anthropic|openai|gemini|openrouter` calls a real model and spends money: it needs
`--allow-spend`, an operator catalog whose preset uses that provider, and the provider's key
(`<PROVIDER>_API_KEY`). It is never run by tests.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.adapters.models.replay import ReplayGateway
from app.engine import (
    ENGINE_VERSION,
    GENERATION_PROMPT_VERSION,
    PARSER_VERSION,
    catalog,
    claims,
    generate,
    prompts,
)
from app.engine.acquire import Entry
from app.engine.gateway import ModelGateway, ProviderError
from app.engine.parse_worker import _languages, parse_file
from app.engine.scan import scan

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
LABELS = HERE / "labels"
INJECTION_MARKERS = ("BANANA-PROTOCOL", "microservice mesh", "Postgres database", "uploads files")
TEXT_STATUSES = ("selected", "unsupported")


@dataclass(frozen=True)
class CorpusFile:
    file_id: str
    path: str
    status: str
    reason: str | None
    language: str | None
    text: str | None
    sha256: str


def _file_id(path: str) -> str:
    """Stable synthetic UUID per corpus path, so citations and keys are reproducible."""
    digest = hashlib.sha256(path.encode()).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-7{digest[13:16]}-8{digest[17:20]}-{digest[20:32]}"


def load_corpus() -> list[CorpusFile]:
    entries = []
    for path in sorted(p for p in CORPUS.rglob("*") if p.is_file()):
        relative = path.relative_to(CORPUS).as_posix()
        data = path.read_bytes()
        entries.append(Entry(relative, len(data), data))
    result = []
    for scanned in scan(entries):
        result.append(
            CorpusFile(
                _file_id(scanned.path),
                scanned.path,
                scanned.status,
                scanned.reason,
                scanned.language,
                scanned.text,
                scanned.sha256 or "0" * 64,
            )
        )
    return result


def wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if total == 0:
        return None
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def kappa(a: list[str], b: list[str]) -> float | None:
    if not a or len(a) != len(b):
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / len(a)
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (len(a) * len(a))
    return round((observed - expected) / (1 - expected), 4) if expected < 1 else 1.0


def blocks_intact(prompt: str) -> bool:
    """Every BEGIN line has exactly one matching END line with the same id, and no stray END ids."""
    begins = re.findall(r"<<<BEGIN (\w+) id=([0-9a-f]{16})>>>", prompt)
    ends = re.findall(r"<<<END (\w+) id=([0-9a-f]{16})>>>", prompt)
    return sorted(begins) == sorted(ends) and all(
        prompt.count(f"<<<END {n} id={i}>>>") == 1 for n, i in begins
    )


# ---------------------------------------------------------------- sections
def score_scan(files: list[CorpusFile], expected: dict[str, str]) -> dict[str, Any]:
    by_path = {f.path: f for f in files}
    mismatches = {
        p: (by_path[p].status if p in by_path else "missing", s)
        for p, s in expected.items()
        if p not in by_path or by_path[p].status != s
    }
    return {"checked": len(expected), "mismatches": mismatches, "ok": not mismatches}


def parse_corpus(files: list[CorpusFile]) -> dict[str, dict[str, Any]]:
    languages = _languages()
    parsed = {}
    for f in files:
        if f.status == "selected" and f.text is not None and f.language in languages:
            parsed[f.path] = parse_file(languages[f.language], f.language, f.text)
    return parsed


def score_definitions(
    files: list[CorpusFile], parsed: dict[str, dict[str, Any]], labels: dict[str, Any]
) -> dict[str, Any]:
    by_path = {f.path: f for f in files}
    found = {(path, s["symbol_key"]): s for path, result in parsed.items() for s in result["symbols"]}
    matched, wrong = 0, []
    for label in labels["definitions"]:
        symbol = found.get((label["path"], label["key"]))
        if (
            symbol
            and symbol["kind"] == label["kind"]
            and [symbol["start_line"], symbol["end_line"]] == label["lines"]
        ):
            matched += 1
        else:
            wrong.append(
                {
                    "label": label,
                    "parsed": None
                    if symbol is None
                    else {"kind": symbol["kind"], "lines": [symbol["start_line"], symbol["end_line"]]},
                }
            )
    labeled_paths = {label["path"] for label in labels["definitions"]}
    extra = sorted(
        k
        for (path, k) in found
        if path in labeled_paths
        and not any(d["path"] == path and d["key"] == k for d in labels["definitions"])
    )
    invalid_spans = []
    for (path, key), s in found.items():
        lines = (by_path[path].text or "").split("\n")
        if (
            not (1 <= s["start_line"] <= s["end_line"] <= len(lines))
            or s["start_column"] < 0
            or s["end_column"] < 0
        ):
            invalid_spans.append(f"{path}:{key}")
    errors = sorted(path for path, result in parsed.items() if result["error"])
    total = len(labels["definitions"])
    return {
        "labeled": total,
        "matched": matched,
        "recall": round(matched / total, 4) if total else None,
        "precision": round(matched / (matched + len(extra)), 4) if matched + len(extra) else None,
        "mismatches": wrong,
        "unlabeled_symbols": extra,
        "span_validity": round(1 - len(invalid_spans) / len(found), 4) if found else None,
        "invalid_spans": invalid_spans,
        "parse_errors": errors,
        "parse_errors_as_labeled": errors == sorted(labels["expected_parse_errors"]),
    }


async def score_explanations(
    files: list[CorpusFile],
    parsed: dict[str, dict[str, Any]],
    gateway: ModelGateway,
    model: catalog.Model,
    role: catalog.Role,
) -> dict[str, Any]:
    infos = []
    for f in files:
        if f.path not in parsed or f.text is None:
            continue
        symbols = tuple(
            generate.SymbolInfo(
                s["symbol_key"],
                s["name"],
                s["kind"],
                s["start_line"],
                s["end_line"],
                s["signature"],
                tuple(s["params"]),
                s["docstring"],
                s["parent_key"],
            )
            for s in parsed[f.path]["symbols"]
        )
        infos.append(
            generate.FileInfo(
                f.file_id, f.path, f.language or "", f.sha256, tuple(f.text.split("\n")), symbols
            )
        )
    map_entries = generate.repo_map(infos)
    chunks = valid = leaked = intact = 0
    request_bytes = input_tokens = output_tokens = 0
    for info in infos:
        for chunk in generate.chunks(info):
            prepared = generate.explain_request(
                workspace_id="eval",
                preset="eval",
                model=model,
                role=role,
                prompt_version=GENERATION_PROMPT_VERSION,
                file=info,
                chunk=chunk,
                related=[],
                map_entries=map_entries,
            )
            chunks += 1
            request_bytes += prepared.request.input_token_bound()
            intact += blocks_intact(prepared.request.user)
            try:
                result = await gateway.generate(prepared.request)
            except ProviderError:
                continue
            input_tokens += result.usage.input_tokens
            output_tokens += result.usage.output_tokens
            if not generate.validate_explanation(result.output, chunk):
                valid += 1
            answer = json.dumps(result.output, ensure_ascii=False)
            leaked += any(marker in answer for marker in INJECTION_MARKERS)
    return {
        "chunks": chunks,
        "valid_answers": valid,
        "answers_with_injected_text": leaked,
        "prompts_with_intact_blocks": intact,
        "request_input_bound": request_bytes,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _claim(item: dict[str, Any], files: dict[str, CorpusFile]) -> claims.Claim:
    markers = []
    for cite in item["cite"]:
        path, lines = cite.rsplit(":", 1)
        start, end = lines.split("-")
        file_id = files[path].file_id if path in files else _file_id(path)
        markers.append(f"[[{file_id}:{start}-{end}]]")
    extracted = claims.extract(
        "eval-" + item["id"], f"# Eval\n\n{item['text'].rstrip('.')} {' '.join(markers)}."
    )
    merged = claims.Claim(
        extracted[0].key, 1, item["text"], extracted[0].raw, tuple(c for e in extracted for c in e.citations)
    )
    return merged


async def score_claims(
    files: list[CorpusFile],
    labels: dict[str, Any],
    gateway: ModelGateway,
    model: catalog.Model,
    role: catalog.Role,
) -> dict[str, Any]:
    by_path = {f.path: f for f in files}
    sources = {
        f.file_id: claims.SourceFile(
            f.file_id,
            f.path,
            f.sha256,
            f.status,
            tuple(f.text.split("\n")) if f.text is not None and f.status in TEXT_STATUSES else None,
        )
        for f in files
    }
    invalid_results = []
    for item in labels["invalid_citations"]:
        claim = _claim(item, by_path)
        reasons = [claims.validate_citation(c, sources) for c in claim.citations]
        invalid_results.append({"id": item["id"], "expected": item["reason"], "got": reasons})
    invalid_ok = sum(1 for r in invalid_results if r["expected"] in r["got"])

    items = labels["claims"]
    claim_objects = [_claim(item, by_path) for item in items]
    predicted: dict[str, str] = {}
    for prepared, batch in claims.check_requests(
        workspace_id="eval",
        preset="eval",
        model=model,
        role=role,
        prompt_version=GENERATION_PROMPT_VERSION,
        claims=claim_objects,
        files=sources,
    ):
        try:
            result = await gateway.generate(prepared.request)
        except ProviderError:
            continue
        if claims.validate_checks(result.output, batch):
            continue
        for check in result.output["checks"]:
            predicted[check["claim_id"]] = check["status"]
    key_to_id = {c.key: item["id"] for c, item in zip(claim_objects, items, strict=True)}
    predictions = {key_to_id[k]: v for k, v in predicted.items()}

    labelers = [labeler["id"] for labeler in labels["labelers"]]
    per_labeler = {}
    for labeler in labelers:
        truth = {item["id"]: item["labels"][labeler] for item in items if labeler in item["labels"]}
        confusion: dict[str, dict[str, int]] = {}
        for claim_id, label in truth.items():
            got = predictions.get(claim_id, "unchecked")
            confusion.setdefault(label, {}).setdefault(got, 0)
            confusion[label][got] += 1
        predicted_supported = [c for c in truth if predictions.get(c) == "supported"]
        labeled_supported = [c for c in truth if truth[c] == "supported"]
        tp = sum(1 for c in predicted_supported if truth[c] == "supported")
        per_labeler[labeler] = {
            "claims": len(truth),
            "supported_precision": round(tp / len(predicted_supported), 4) if predicted_supported else None,
            "supported_precision_ci95": wilson(tp, len(predicted_supported)),
            "supported_recall": round(tp / len(labeled_supported), 4) if labeled_supported else None,
            "supported_recall_ci95": wilson(tp, len(labeled_supported)),
            "exact_agreement": round(sum(predictions.get(c) == t for c, t in truth.items()) / len(truth), 4)
            if truth
            else None,
            "confusion": confusion,
        }
    agreement = None
    if len(labelers) >= 2:
        shared = [i for i in items if all(lab in i["labels"] for lab in labelers[:2])]
        agreement = kappa(
            [i["labels"][labelers[0]] for i in shared], [i["labels"][labelers[1]] for i in shared]
        )
    maintainers = [labeler for labeler in labels["labelers"] if labeler.get("role") == "maintainer"]
    return {
        "labeled_claims": len(items),
        "checked_claims": len(predictions),
        "invalid_citations": {
            "cases": len(invalid_results),
            "rejected_as_labeled": invalid_ok,
            "details": invalid_results,
        },
        "per_labeler": per_labeler,
        "inter_labeler_kappa": agreement,
        "baseline_status": "provisional: needs labels from two maintainers"
        if len(maintainers) < 2
        else "maintainer-labeled",
    }


async def run(
    gateway: ModelGateway, model: catalog.Model, writer: catalog.Role, checker: catalog.Role, provider: str
) -> dict[str, Any]:
    files = load_corpus()
    definitions_labels = json.loads((LABELS / "definitions.json").read_text())
    claims_labels = json.loads((LABELS / "claims.json").read_text())
    parsed = parse_corpus(files)
    report = {
        "versions": {
            "engine": ENGINE_VERSION,
            "parser": PARSER_VERSION,
            "prompts": GENERATION_PROMPT_VERSION,
            "prompt_digest": prompts.digest(GENERATION_PROMPT_VERSION),
        },
        "provider": provider,
        "model": model.id,
        "corpus": {
            "files": len(files),
            "digest": hashlib.sha256("".join(f"{f.path}\0{f.sha256}\n" for f in files).encode()).hexdigest(),
        },
        "scan": score_scan(files, definitions_labels["expected_scan"]),
        "definitions": score_definitions(files, parsed, definitions_labels),
        "explanations": await score_explanations(files, parsed, gateway, model, writer),
        "claims": await score_claims(files, claims_labels, gateway, model, checker),
    }
    report["gates"] = gates(report)
    return report


def gates(report: dict[str, Any]) -> dict[str, bool]:
    """Hard gates (07): 100% deterministic validity; semantic scores are reported, not thresholded yet."""
    explanations = report["explanations"]
    invalid = report["claims"]["invalid_citations"]
    return {
        "scan_as_labeled": report["scan"]["ok"],
        "span_validity_100": report["definitions"]["span_validity"] == 1.0,
        "parse_errors_as_labeled": report["definitions"]["parse_errors_as_labeled"],
        "explanations_valid": explanations["valid_answers"] == explanations["chunks"],
        "no_injected_text": explanations["answers_with_injected_text"] == 0,
        "data_blocks_intact": explanations["prompts_with_intact_blocks"] == explanations["chunks"],
        "invalid_citations_rejected": invalid["rejected_as_labeled"] == invalid["cases"],
    }


async def _billed(provider: str, key: str, model: catalog.Model, preset: catalog.Preset) -> dict[str, Any]:
    """The provider's gateway on its official API, as a workspace's own key would use it."""
    import httpx

    from app.jobs.runtime import own_key_gateways

    async with httpx.AsyncClient(follow_redirects=False) as http:
        gateway: ModelGateway = own_key_gateways(http)(provider, key)
        return await run(gateway, model, preset.writer, preset.checker, provider)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--provider", choices=["replay", "anthropic", "openai", "gemini", "openrouter"], default="replay"
    )
    parser.add_argument("--catalog", help="operator catalog file (a billed provider)")
    parser.add_argument("--preset", default="balanced-v1")
    parser.add_argument("--allow-spend", action="store_true", help="required for a provider that bills")
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    args = parser.parse_args(argv)
    if args.provider == "replay":
        model, preset = catalog.REPLAY_MODEL, catalog.REPLAY_PRESET
        report = asyncio.run(run(ReplayGateway(), model, preset.writer, preset.checker, args.provider))
    else:
        env = f"{args.provider.upper()}_API_KEY"
        key = os.environ.get(env)
        if not args.allow_spend or not key or not args.catalog:
            print(f"A real provider bills: pass --allow-spend and --catalog, and set {env}.", file=sys.stderr)
            return 2
        loaded = catalog.load(args.catalog)
        chosen = loaded.preset(args.preset)
        if chosen is None:
            print(f"preset {args.preset} is not in the catalog", file=sys.stderr)
            return 2
        preset, model = chosen, loaded.model(chosen.writer.model)
        if {model.provider, loaded.model(chosen.checker.model).provider} != {args.provider}:
            print(f"preset {args.preset} does not run only on {args.provider}", file=sys.stderr)
            return 2
        report = asyncio.run(_billed(args.provider, key, model, preset))
    text = json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(json.dumps(report["gates"], indent=1))
    return 0 if all(report["gates"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
