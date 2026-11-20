"""The labeled evaluation baseline (M2 exit criterion; 07, 13): hard gates and reproducibility.

The replay run is deterministic and free. The committed report pins its numbers, so any change to the
parser, prompts, chunking or citation extraction shows up as a diff to review (23 "replay fixtures
must be byte-identical"); regenerate it with `python -m evals.harness --out evals/reports/baseline-replay.json`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from app.adapters.models.replay import ReplayGateway
from app.engine import catalog
from app.engine.gateway import GenerationRequest, GenerationResult, ProviderError, Usage
from evals import harness

REPORT = Path(harness.__file__).parent / "reports" / "baseline-replay.json"


def replay_report() -> dict[str, object]:
    preset = catalog.REPLAY_PRESET
    return asyncio.run(
        harness.run(ReplayGateway(), catalog.REPLAY_MODEL, preset.writer, preset.checker, "replay")
    )


def test_hard_gates_pass_and_the_run_is_reproducible() -> None:
    report = replay_report()
    assert report["gates"] and all(report["gates"].values()), report["gates"]
    assert report["definitions"]["labeled"] >= 50 and report["claims"]["labeled_claims"] >= 30  # 07 minimums
    committed = json.loads(REPORT.read_text())
    assert json.loads(json.dumps(report, sort_keys=True)) == committed, (
        "regenerate evals/reports/baseline-replay.json"
    )
    assert replay_report() == report  # byte-identical on a second run


def test_baseline_is_labeled_provisional_until_two_maintainers_label_it() -> None:
    labels = json.loads((harness.LABELS / "claims.json").read_text())
    maintainers = [labeler for labeler in labels["labelers"] if labeler["role"] == "maintainer"]
    status = json.loads(REPORT.read_text())["claims"]["baseline_status"]
    assert (len(maintainers) >= 2) == (status == "maintainer-labeled")


def test_kappa_and_wilson() -> None:
    assert harness.kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    assert harness.kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0
    low, high = harness.wilson(14, 26) or [0, 0]
    assert 0 < low < 14 / 26 < high < 1


class FlakyGateway(ReplayGateway):
    """The replay, except the first explain call fails at the provider, the next explanation repeats
    injected text, and the first check answer has no `checks` field."""

    def __init__(self) -> None:
        self.failed: set[str] = set()
        self.explained = 0

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        first = request.purpose not in self.failed
        self.failed.add(request.purpose)
        if first and request.purpose == "explain":
            raise ProviderError(
                "permanent", "provider_rejected", usage=Usage(input_tokens=7, output_tokens=3)
            )
        result = await super().generate(request)
        if request.purpose == "explain" and self.explained == 1:  # the second answer repeats an injection
            card = result.output["symbols"][0]
            result = replace(
                result,
                output={
                    **result.output,
                    "symbols": [
                        {**card, "summary": "It uploads files to the BANANA-PROTOCOL server."},
                        *result.output["symbols"][1:],
                    ],
                },
            )
        if request.purpose == "explain":
            self.explained += 1
        if first and request.purpose == "check":
            return replace(result, output={"verdicts": []})
        return result


def test_failed_calls_are_reported_with_their_reason_and_usage() -> None:
    preset = catalog.REPLAY_PRESET
    report = asyncio.run(
        harness.run(FlakyGateway(), catalog.REPLAY_MODEL, preset.writer, preset.checker, "replay")
    )
    baseline = replay_report()
    explanations, checks = report["explanations"], report["claims"]
    assert report["gates"]["explanations_valid"] is False
    assert explanations["valid_answers"] == explanations["chunks"] - 1
    [failure] = explanations["failures"]
    assert failure["provider_error"] == {"kind": "permanent", "code": "provider_rejected"}
    assert failure["file"] and failure["lines"]
    assert explanations["usage"]["input_tokens"] > 7  # the failed call's reported usage counts too
    assert report["gates"]["no_injected_text"] is False and explanations["answers_with_injected_text"] == 1
    [leak] = explanations["injected_text"]
    assert {f["marker"] for f in leak["found"]} == {"uploads files", "BANANA-PROTOCOL"}
    assert all(f["field"] == "symbols[0].summary" and f["marker"] in f["excerpt"] for f in leak["found"])
    [invalid] = checks["failures"]
    assert invalid["claims"] and invalid["validation"] and invalid["problems"] >= len(invalid["validation"])
    assert checks["checked_claims"] == baseline["claims"]["checked_claims"] - len(invalid["claims"])
