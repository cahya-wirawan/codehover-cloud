# Evaluation corpus and baseline

This is the labeled evaluation required by `build-package/07_AI_OR_AUTOMATION_PIPELINE.md` ("Evaluation corpus and release gates") and test plan row "AI eval".

## Contents

- `corpus/`: redistributable fixtures written for this project. They are repository data, never linted or executed.
  - `pylib/`: a small Python library with classes, methods, nested functions, decorators, async functions, a redefinition, Unicode identifiers, a malformed file, a secret-like value and an injected README and comment.
  - `tsapp/`: a TypeScript/React app with interfaces, type aliases, function-valued consts, a class, a JS generator, a vendored `node_modules` directory and an injected README.
- `labels/definitions.json`: 50 labeled definitions (key, kind, lines), plus the expected scan and parse-error outcomes.
- `labels/claims.json`: 34 labeled guide claims about the corpus, and four citations that must fail deterministic validation. The claims mix supported, partial and unsupported statements, including false architecture claims, claims inferred from names alone, and claims that carry injected instructions.
- `harness.py`: runs scan, parse, explanation and checker on the corpus and writes a report.
- `reports/baseline-replay.json`: the committed baseline of the deterministic replay run. CI regenerates it and fails on any difference.

## Status: provisional

Every label is a draft by the implementing assistant (`draft-claude`). Spec 07 requires at least 50 definitions and 30 claims labeled by two maintainers before the baseline counts for beta. Until then the report says `"baseline_status": "provisional: needs labels from two maintainers"`. No accuracy claim may be made from it.

To complete the labeling:

1. Each maintainer adds an entry to `labelers` with `"role": "maintainer"` and a unique `id`.
2. Each maintainer labels every claim in `claims.json` under `labels.<their id>`, independently and without looking at the other labels or the model output. Each also reviews `definitions.json`.
3. Run the harness. The report shows each labeler's precision and recall with 95% Wilson intervals, the confusion matrix, and Cohen's kappa between the first two labelers. Resolve disagreements, record them, and commit the new report.

## Running

```bash
cd backend
.venv/bin/python -m evals.harness --out evals/reports/baseline-replay.json   # free, deterministic, run by CI
```

A real model run bills, and is never run by tests:

```bash
ANTHROPIC_API_KEY=... .venv/bin/python -m evals.harness --provider anthropic \
  --catalog ../deployment/model-catalog.json --preset balanced-v1 --allow-spend --out evals/reports/<preset>.json
```

The corpus needs about 60 KB of input, as the report's `request_input_bound` shows, plus outputs, for one explain pass and one check pass.

## Gates

Hard gates, all 100%: scan as labeled, span validity, parse errors as labeled, valid explanation answers, no injected text in answers, intact data blocks in every prompt, and invalid citations rejected with the labeled reason.

Semantic scores (supported-label precision and recall, agreement) are reported, not thresholded, until a maintainer baseline exists. The release target in 07 is supported-label precision of at least 0.95, with its confidence interval reported.

The replay checker only matches identifiers. Its baseline precision of about 0.54 against the draft labels is why the catalog refuses a preset that pairs a model writer with the replay checker.
