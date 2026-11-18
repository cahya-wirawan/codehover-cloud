"""Claim extraction, deterministic citation validation, checker/repair validation and the gate (T12; 07)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine import claims, generate

F1 = "019a90a0-0000-7000-8000-000000000001"
F2 = "019a90a0-0000-7000-8000-000000000002"
FILES = {
    F1: claims.SourceFile(F1, "src/app.py", "a" * 64, "selected", tuple(f"line {i}" for i in range(1, 21))),
    F2: claims.SourceFile(F2, ".env", "b" * 64, "excluded", None),
}
PAGE = f"""# Flow

The entry point parses arguments [[{F1}:1-3]]. It then calls the helper [[{F1}:4-4]]. Nothing else happens.

- Configuration is read once [[{F1}:5-6]]
- Untouched list item without citation

```python
def not_a_claim():  # [[{F1}:1-1]] inside code is ignored
```

Errors are logged. [[{F1}:7-8]] Secrets come from the environment [[{F2}:1-1]].
"""


@pytest.mark.parametrize("name", ["claim-check", "claim-repair"])
def test_engine_schemas_equal_the_spec_contracts(name: str) -> None:
    spec = Path(__file__).resolve().parents[3] / "build-package" / "contracts" / f"{name}.schema.json"
    assert json.loads(spec.read_text()) == generate.schema(name)


def test_extraction_pairs_each_cited_sentence_or_item_with_its_ranges() -> None:
    extracted = claims.extract("flow", PAGE)
    assert [c.text for c in extracted] == [
        "The entry point parses arguments .",
        "It then calls the helper .",
        "Configuration is read once",
        "Errors are logged.",  # the citation after the full stop belongs to this sentence
        "Secrets come from the environment .",
    ]
    assert [[(x.start_line, x.end_line) for x in c.citations] for c in extracted] == [
        [(1, 3)],
        [(4, 4)],
        [(5, 6)],
        [(7, 8)],
        [(1, 1)],
    ]
    assert [c.ordinal for c in extracted] == [1, 2, 3, 4, 5]
    assert len({c.key for c in extracted}) == 5
    assert claims.extract("flow", PAGE)[0].key == extracted[0].key  # deterministic
    assert claims.extract("other", PAGE)[0].key != extracted[0].key  # keyed by page slug too
    assert (
        claims.uncited_sentences(PAGE) == 1
    )  # the uncited list item; sentences under four words are not counted


@pytest.mark.parametrize(
    ("marker", "reason"),
    [
        (f"[[{F1}:2-4]]", None),
        (f"[[{F1}:0-4]]", "range_out_of_bounds"),
        (f"[[{F1}:5-30]]", "range_out_of_bounds"),
        (f"[[{F1}:9-3]]", "range_out_of_bounds"),
        (f"[[{F2}:1-1]]", "file_excluded"),  # excluded files can never be cited
        ("[[019a90a0-0000-7000-8000-0000000000ff:1-1]]", "file_not_in_snapshot"),
        ("[[src/app.py:1-3]]", "citation_syntax"),  # paths are not immutable file IDs
        ("[[see above]]", "citation_syntax"),
    ],
)
def test_deterministic_citation_validation(marker: str, reason: str | None) -> None:
    (claim,) = claims.extract("p", f"# P\n\nA claim {marker}.")
    assert claims.validate_citation(claim.citations[0], FILES) == reason


def test_checker_answers_are_bounded_to_supplied_claims_and_cited_lines() -> None:
    batch = claims.extract("flow", PAGE)[:2]
    ok = {
        "checks": [
            {"claim_id": batch[0].key, "status": "supported", "reason": "r", "evidence_line_numbers": [1, 2]}
        ]
    }
    assert claims.validate_checks(ok, batch) == []  # a missing answer becomes unchecked, not a repair
    stray = {
        "checks": [
            {"claim_id": batch[0].key, "status": "supported", "reason": "r", "evidence_line_numbers": [9]}
        ]
    }
    assert claims.validate_checks(stray, batch)
    foreign = {
        "checks": [{"claim_id": "0" * 32, "status": "supported", "reason": "r", "evidence_line_numbers": []}]
    }
    assert claims.validate_checks(foreign, batch)
    label = {
        "checks": [
            {"claim_id": batch[0].key, "status": "probably", "reason": "r", "evidence_line_numbers": []}
        ]
    }
    assert claims.validate_checks(label, batch)


def test_check_requests_carry_only_claims_and_cited_lines() -> None:
    from app.engine import catalog

    valid = [
        c
        for c in claims.extract("flow", PAGE)
        if all(claims.validate_citation(x, FILES) is None for x in c.citations)
    ]
    ((prepared, batch),) = claims.check_requests(
        workspace_id="w",
        preset="replay-v2",
        model=catalog.REPLAY_MODEL,
        role=catalog.REPLAY_PRESET.checker,
        prompt_version="0.3.0",
        claims=valid,
        files=FILES,
    )
    assert [c.key for c in batch] == [c.key for c in valid]
    assert "line 1" in prepared.request.user and "line 20" not in prepared.request.user  # only cited lines
    assert prepared.request.purpose == "check" and prepared.request.role == catalog.REPLAY_PRESET.checker


def test_repair_validation_and_application() -> None:
    extracted = claims.extract("flow", PAGE)
    flagged = extracted[:2]
    replace = {
        "replacements": [
            {
                "claim_id": flagged[0].key,
                "text": "The entry point reads its arguments",
                "action": "replace",
                "citations": [{"file_id": F1, "start_line": 1, "end_line": 2}],
            },
            {"claim_id": flagged[1].key, "text": "", "action": "remove", "citations": []},
        ]
    }
    assert claims.validate_repair(replace, flagged) == []
    repaired = claims.apply_repair(PAGE, extracted, replace)
    assert f"The entry point reads its arguments [[{F1}:1-2]]." in repaired
    assert "calls the helper" not in repaired and "Configuration is read once" in repaired
    widened = {
        "replacements": [
            {**replace["replacements"][0], "citations": [{"file_id": F1, "start_line": 1, "end_line": 9}]}
        ]
    }
    assert claims.validate_repair(widened, flagged)  # may not cite beyond the claim's cited lines
    invented = {"replacements": [{**replace["replacements"][0], "claim_id": extracted[3].key}]}
    assert claims.validate_repair(invented, flagged)  # only flagged claims may change
    uncited = {"replacements": [{**replace["replacements"][0], "citations": []}]}
    assert claims.validate_repair(uncited, flagged)


def test_publication_gate() -> None:
    counts = {"supported": 4, "partial": 1, "unsupported": 0, "unchecked": 0, "invalid": 0}
    assert claims.publication_blockers(counts, 0, 0) == []
    assert claims.publication_blockers({**counts, "invalid": 1}, 0, 0) == ["invalid_citations"]
    assert claims.publication_blockers({**counts, "unchecked": 1}, 0, 0) == ["unchecked_claims"]
    assert claims.publication_blockers({**counts, "unsupported": 2}, 1, 0) == ["unsupported_claims"]
    assert claims.publication_blockers({**counts, "unsupported": 2}, 2, 0) == []  # all waived
    assert claims.publication_blockers(counts, 0, 1) == ["failed_pages"]
    assert claims.page_status(["supported", "partial"]) == "partial"
    assert claims.page_status(["supported", "invalid", "unchecked"]) == "invalid"
    assert claims.page_status([]) == "unchecked"


def test_search_terms_and_queries() -> None:
    from app.engine import search

    terms = search.identifier_terms("parse_shape", "ParseShape.toJSON").split()
    assert {"parse", "shape", "parseshape", "to", "json", "tojson"} <= set(
        terms
    )  # whole names match by name_lower
    assert search.identifier_terms("größe_in_metern").split()[:3] == ["größe", "in", "metern"]
    assert search.tsquery("foo bar") == "foo:* & bar:*"
    assert search.tsquery("a & b | !c ) ( : *") == "a:* & b:* & c:*"  # operators never pass through
    assert search.tsquery("&|!()") is None
    assert search.prose("# T\n\nRuns `main` [[x:1-2]].") == "T Runs main ."
