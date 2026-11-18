# Generation and automation pipeline

## Immutable pipeline

Acquisition → safe scan → parse/reference graph → rank → estimate/approve → per-file explanations → overview → guide plan → guide pages → deterministic citation validation → independent semantic support checks → sanitize/render → manifest → ready draft. Publication is separate. P1 translation runs after verified canonical output; type-aware indexing is a constrained optional stage before ranking.

Each stage persists typed inputs, digests, outputs and terminal status. Engine emits versioned JSON/Markdown artifacts rather than assuming a local CLI cache layout. Code content is data, even when comments or README contain instructions. Neither generation nor checking has tools, shell, secrets or outbound browsing. Only provider gateway can contact allowlisted model endpoints.

## Stage contracts

| Stage | Input | Output and validation |
|---|---|---|
| acquire/scan | verified repository ID, exact commit | immutable snapshot, SHA256 files, excluded reasons; enforce compressed/expanded/file/depth limits |
| parse | UTF-8 source + language (sandboxed runner, no network/credentials) | definitions, spans, parent scope, references; tree-sitter UTF-8 byte columns converted once to Unicode scalar offsets (0-based, end-exclusive); malformed parser result falls back to source-only |
| rank | reference graph + safe repo map | stable priority list, centrality with entry-point boost/test downweight; deterministic tie-break path |
| explain | numbered file chunks, bounded repo map, README excerpt | strict Explanation schema; real symbol IDs only, valid line bounds, no HTML |
| overview | summaries and repo map | overview, concepts, reading order of selected valid paths |
| plan | overview, symbols, summaries | 3–8 unique safe slugs, page purpose, bounded source paths/ranges |
| guide | plan + selected source evidence | allowlisted Markdown, valid citation markers, bounded Mermaid |
| check | extracted claims + exact immutable cited lines | deterministic status + semantic supported/partial/unsupported/unchecked, reason and evidence ID |
| render | valid versioned output bundle | escaped code, sanitized prose, local assets, manifest and optional static ZIP/KB |

The estimate runs on the scanned snapshot before parsing. It therefore selects files with one deterministic file-level scope function (include/exclude globs, then non-test files, shallower paths and path order up to `max_files`), and the build explains exactly that selection; ranking orders work inside it. Token counts are byte- and line-based bounds reported as a low/high range, priced from the catalog revision named in the estimate.

File chunks break at top-level definitions, target 400–500 lines, hard request token cap from model catalog. Each chunk returns its own Explanation; the file card shows the first chunk's summary followed by later chunk summaries in source order, with no extra model call in MVP. Very large definitions get bounded subranges with explicit incomplete-context annotation; no silent truncation described as complete. Excluded files do not enter repo maps, summaries, guides or provider prompts. Reference context has explicit file/source version and resolution basis; a name match is not claimed as type-confirmed.

## Provider abstraction

`ModelGateway.generate(request, budget_reservation) -> StructuredResult`; `count_tokens`, `capabilities`, optional `submit_batch/get_batch/cancel_batch`. Request carries provider/model IDs, temperature if supported, max output tokens, stage, prompt version and structured schema. Result carries provider request ID, actual usage including cached tokens, normalized error, output hash and provenance. Approved catalog binds models to context limits, structured-output capabilities, approved data region/retention policy and price revision. No guessed default model names or inherited CLI price table. Start with one approved provider and a deterministic replay adapter. M1 implements the replay generator `replay-v1` (prompt set 0.1.0): cards are derived from parsed source only (docstring, signature, parameters) and validated at runtime against the explanation schema v2, the same validator model output must pass. The M1 stages are acquire, index (sandboxed tree-sitter parse, name-based references, ranking), explain and render (manifest last); adapters for Anthropic/OpenAI-compatible/self-hosted are separate implementations.

Server selects endpoints from operator config. User-supplied provider base URLs are disallowed in hosted SaaS to prevent SSRF. BYOK P1 stores envelope-encrypted secrets server-side and exposes only masked metadata. Batch mode is P1: persist provider batch IDs before polling, reserve their entire maximum budget, never silently fall back to interactive rates.

## Caching and incremental correctness

Cache key includes workspace ID, source digest, source range, referenced context digest, repo map/README digest, parser/engine `MAJOR.MINOR` and artifact schema `$id`, provider/model preset, prompt-set `MAJOR.MINOR` plus prompt template digest (versioning rules in document 23), language/level, selected file set and relevant generation settings. Reuse never crosses workspace boundary. Changed referenced definitions invalidate dependent explanations; guide pages invalidate when any cited file/range or dependency summary changes. MVP may conservatively invalidate all affected context when computing exact dependencies is expensive. Content-only file caching is insufficient when meaning depends on other files. Scope exclusion changes purge incompatible cache entries and prevent excluded content from reappearing in a build.

## Verification and publication quality

Claims are extracted deterministically after guide generation: each sentence or list item carrying at least one citation marker is one claim, keyed by a hash of page slug, ordinal and normalized text, and paired with each cited range (one `claim_checks` row per claim and citation). First verify file exists, hash matches snapshot, line/column bounds, citation syntax and snippet consistency. Invalid deterministic citations block publication and cannot be waived. Next independent checker receives claim plus cited lines, not the writer's justification. Status values: supported (fully entailed), partial (only part entailed), unsupported (not supported or contradicted), unchecked (checker unavailable), invalid (source citation invalid). Store reasons and checker metadata; do not turn a model label into proof. Checker may use a separate model/prompt, with distinct request context. Same-provider correlation remains a known risk.

Default publication gate requires all scheduled checks executed, zero invalid citations, and no unwaived unsupported claims. If the checker stays unavailable after its bounded retries, the build still finishes as a `ready` draft whose quality report marks those claims `unchecked`, which blocks publication. Because terminal builds never re-run, completing the checks means a retry build: it reuses tenant-local cached explanations and guide pages for the same snapshot and configuration, so normally only the check stage spends budget. Partial claims remain visible and labeled; admin must acknowledge the summary before publish. One repair cycle may rewrite/remove unsupported claims, then rerun all changed checks. Do not delete evidence or hide failed checks to improve the score. Coverage uses all extracted claims including failed/unchecked ones as denominator; report uncited sentence sampling separately so citing fewer claims cannot masquerade as higher faithfulness.

## Budgets and latency

Reserve the approved build cap once at build creation; before each request atomically earmark its maximum token cost within that held envelope. Settle actual usage and release the call earmark with one idempotent ledger transaction; unused capacity stays available to later stages until build termination. Do not reserve the same amount twice at workspace level (see document 11). Never exceed cap by racing multiple tasks. Support output/cache-token price differences in immutable rate snapshot. If no room remains for mandatory guide/check stages, stop with budget_exhausted; retain draft and explain remaining work. Cancellation cannot guarantee an in-flight provider stops billing. Timeout with uncertain provider outcome is held as pending reconciliation; conservative reservation remains until resolved or a documented adjustment.

Initially allow at most 4 concurrent provider calls per build, and the active-build limits from document 01 (one per project, two per workspace by default, lower where the plan says so). Provider rate limits and the global worker pool bound this further. Fail-fast on auth errors, circuit-break on outages, retry 429/5xx according to persisted policy; maximum three transient retries and one schema repair, each budgeted. Display provider waiting and unknown completion times honestly. Model quality/config changes require eval gate and a new preset version.

## Evaluation corpus and release gates

Maintain redistributable fixtures: small Python library; TS/React application; nested scopes/overloads; Unicode identifiers; malformed source; malicious comments/README; false architecture claims; missing/invalid citations; secret-like strings and excluded directories. Store synthetic/replay outputs in test fixtures, not real private source in analytics. At least 50 definitions and 30 guide claims manually labeled by two maintainers for beta; expand before broad language claims. Metrics: span validity 100%, exact-name search MRR@10, Recall@10, ambiguity calibration, supported-claim precision/recall against humans, agreement, actionable usefulness, injection resistance, token/cost regression and version reproducibility. No hard minimum semantic score is claimed before baseline; initial release target supported-label precision ≥0.95 on labeled corpus with CI reported and zero invalid citations. A failed or too-small evaluation blocks broad accuracy marketing.

Prompts and schemas are in document 19 and `contracts/explanation.schema.json`. Logs record versions, digests, counts and timings; prompt/source/body logging is disabled by default.
