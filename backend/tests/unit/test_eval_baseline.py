"""The labeled evaluation baseline (M2 exit criterion; 07, 13): hard gates and reproducibility.

The replay run is deterministic and free. The committed report pins its numbers, so any change to the
parser, prompts, chunking or citation extraction shows up as a diff to review (23 "replay fixtures
must be byte-identical"); regenerate it with `python -m evals.harness --out evals/reports/baseline-replay.json`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.adapters.models.replay import ReplayGateway
from app.engine import catalog
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
