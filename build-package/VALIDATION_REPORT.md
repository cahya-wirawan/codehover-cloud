# Package validation

Date: 2026-11-21 · Package version 0.5.37

0.5.37 stops writer stages when the provider never answers. A live Anthropic build from the local stack ended `ready` with only source-only text: every request over about 1.5 KB was reset on the network's IPv4 route, each call was retried four times as `unknown`, and the 20 attempts held $1.70. `tests/integration/test_generation.py` now checks that a single unknown overview call is not retried within the call, stops the stage and succeeds on the task's retry, holding one maximum. With every explain call unknown, the build fails after the task's attempts, makes one call per chunk per attempt, and writes no fallback explanation. Results: integration 234 passed, unit 250 passed (8 skipped).

0.5.36 raises the estimate's high bound for reasoning models. A live OpenRouter build (Qwen4 Max, medium effort) with its cap left at the high bound ($0.29) passed every stage through verify and then ended as `budget_exhausted` in the check stage with $0.21 spent and nothing in flight: one explanation used 9,370 output tokens against an assumed 2,500, and the one-file scope was estimated at one guide page while the plan wrote two. With the same catalog, the high bound for that scope is now $1.83, and the same build with a $0.50 cap finished. `tests/unit/test_catalog_estimate.py` checks that a one-file scope counts five pages (`guide_pages`), that a role with an effort counts every call at its `max_output_tokens`, that a role without one keeps the typical sizes, and that the low bound counts the fewest pages. Results: integration 233 passed, unit 250 passed (8 skipped).

0.5.35 fixes budget holds, as seen in live builds from the local stack. An Anthropic build was refused at the TLS handshake and then ended as `budget_exhausted` after spending nothing. An OpenRouter build ended the same way with $0.05 spent of a $0.29 cap, because 4 concurrent worst-case holds did not fit.
- `tests/integration/test_generation.py`:
  - With a cap that fits every explain call one at a time but not two worst cases at once, the three concurrent explain calls wait for each other and all succeed.
  - A call cut off by a worker shutdown becomes `unknown` (`call_cancelled`) with its hold kept, and the stage makes no further call.
  - An unknown attempt is left alone by the sweep while it is recent. Once it is 25 hours old, the sweep settles it at its maximum as `reconciled`: the build's reserved amount goes to 0 and the ledger balances. A second sweep changes nothing.
- `tests/unit/test_bounded.py`: after a stop, running units finish, queued units never start, the first stop is the one raised, and cancelling cancels every unit.
- `tests/unit/test_generation_engine.py`: Anthropic connect errors and connect timeouts are transient, while dropped connections and write timeouts stay unknown.

Results: integration 233 passed, unit 249 passed (8 skipped).

0.5.34 refuses generation builds whose cap would be zero. In a local stack with `GENERATION_ALLOWANCE_MICRO_USD=0`, such builds started, reserved nothing and stopped at once as `budget_exhausted` ("spending cap reached"). `tests/integration/test_budgets.py` now checks that, with no allowance left, a start without a cap is refused with 409 `ALLOWANCE_EXCEEDED` and `available_micro_usd: 0`, and that an explicit cap of zero is refused with 422 `CAP_TOO_LOW`. Neither creates a build or a ledger entry. Results: the budget suite passed (12 tests), and unit tests passed (241, 8 skipped). Separately, live eval runs on 2026-11-20 passed all hard gates with one preset per provider (OpenAI, Anthropic, Gemini, OpenRouter), after the fixes in parser 0.4.0.

0.5.33 adds OpenAI, Gemini and OpenRouter as providers. `tests/unit/test_model_adapters.py` checks each adapter against `httpx.MockTransport`:
- **Requests.** Each adapter calls only its official endpoint, or the operator's `OPENAI_BASE_URL` for the operator's OpenAI key. Each sends the strict schema projection, the output cap and the effort, and Gemini sends the key in a header, not the URL. OpenRouter sends `max_price` equal to the catalog price, `data_collection: deny`, `require_parameters` and no `models` list.
- **Usage.** Cached input is counted apart, and reasoning or thinking tokens count as output.
- **Failures.** Truncation, refusals, safety stops, blocked prompts, non-JSON answers, quota and auth errors, rate limits, OpenRouter errors inside a 200, and transport failures map onto the gateway's error kinds. Response text never appears in the error.
- **Key checks.** Each provider's check calls its official API and raises on an outage. An OpenRouter management key is refused.
- **Catalog.** A `batch_price` on a non-Anthropic model and Gemini `max` effort are refused.

In `tests/integration/test_own_keys_batch.py`, all three new providers store checked keys (migration 0030), and an own-key build on an OpenAI preset opens the workspace's OpenAI key once and finishes ready. Results: integration 229 passed, unit 237 passed (7 skipped), frontend 44 passed. The adapters have not been run against the live provider APIs.

0.5.32 mounts the model catalog in Compose. `docker compose config` resolves the read-only bind of `deployment/catalog` into the API and the worker, and both deployment copies are identical. In a one-off API container from the local image, a copy of the example catalog placed in the folder loaded through `MODEL_CATALOG_FILE=/etc/codehover/catalog/…` (presets `replay-v2` and `balanced-v1`), and writing into the folder was refused as a read-only file system. No application code changed.

0.5.31 adds the operator's `ANTHROPIC_BASE_URL` and pins every Anthropic client's endpoint. `tests/unit/test_anthropic_endpoint.py` (4 tests):
- **The setting.** It accepts only https URLs without credentials, a query or a fragment; blank means the official API.
- **No SDK fallback.** With `ANTHROPIC_BASE_URL` pointing elsewhere in the environment, a gateway created without an endpoint still calls the official API.
- **Operator key only.** The worker runtime gives the operator's gateway the configured endpoint, while a gateway for a workspace's own key, and the check when an owner saves one, use the official API.
- Full suites: backend unit 196 (macOS). No database, API or frontend code changed.

0.5.30 adds license-gated public sites of public-source repositories (FR-029), run locally against the fake of GitHub's public endpoints, which now answers the license endpoint:
- `tests/integration/test_publication_licenses.py` (9 tests):
  - **Permitted.** An MIT license at the build's commit allows a public site. GitHub is asked with `ref` set to that exact commit. The site info names the license, its `file_id` opens the license text in the published code view, and `publication.changed` audits it.
  - **Refused.** No license, `NOASSERTION`, `BUSL-1.1`, and a license file the build lacks are each 409 `LICENSE_NOT_PERMITTED` with their reason, and no publication is written. A private site of the same build publishes without asking GitHub.
  - **Turned private.** A repository that turned private is 409 `REPOSITORY_NOT_PUBLIC`, before the license is looked up.
  - **Backstop.** The database trigger refuses making such a publication public, or clearing its license, outside the service.
  - **Lifted suspensions.** An unlicensed public site suspended for billing or by an operator (the state such a site had if it was already suspended when 0029 arrived) stays offline as `license` when that suspension is lifted, and serves again only after a publish with the check.
- `tests/unit/test_public_github.py`: the license call re-checks visibility first, sends the commit as `ref`, maps a missing license to none and an unnamed one to `NOASSERTION`, and accepts only commit SHAs.
- Unchanged App-project publishing, billing suspensions and operator takedowns pass `test_publication.py`, `test_billing.py` and `test_operations.py` under the new trigger.
- Full suites: backend unit 192 (macOS), integration 228, frontend 43.

0.5.29 adds Rust (T23). The T23 gate, "no network/install/hooks; timeout fallback labeled", was run in the backend image under the Compose hardening (read-only root, all capabilities dropped, no-new-privileges, `--network none`):
- **Indexer sandbox** (`tests/unit/test_rust.py`, Linux): in the indexer profile with rust-analyzer as its program, the probe opens no socket and reads no parent environment, `/etc` or secrets. It cannot start Python or `/bin/sh` but can start rust-analyzer, and writes only to the scratch `out`. The Node probe is unchanged and runs once for both npm indexers.
- **Real indexer:**
  - **Cross-crate.** A two-crate Cargo workspace resolves across crates through a renamed path dependency (`geo = { package = "geo-core" }`).
  - **Through std.** It resolves through std types: `Option::map` on a `Vec`'s slice, and slice iterators with a closure.
  - **By receiver.** A trait method resolves to `Circle`'s impl, not `Square.area` of the same name.
  - **Test code.** Code under `cfg(test)` is analyzed.
  - **Nothing else runs.** rust-analyzer's own attempts to run `rustc` and `cargo` are refused by the sandbox, and it then reads the library sources directly.
  - **Speed and fallback.** It takes about 6 s. A zero timeout falls back, and the scratch checkout is removed.
- **Crate graph** (`tests/unit/test_rust.py`): Cargo's target discovery, workspace edition inheritance, `[lib]` names and paths, dev and build dependencies, and feature cfgs. Unreadable and virtual manifests add no crates. Roots outside the checkout (`..`, absolute) are refused.
- **Parser:** Rust items map onto the existing kinds; impl methods are named after their type; doc comments go through attributes. Syntax errors still parse partially.
- **Stage** (`tests/integration/test_builds.py`): a Rust repository builds to ready. The index stage gives the indexer exactly the snapshot's `Cargo.toml` files, and resolved references are stored as `index` edges.
- **Worker startup:** the rebuilt stack's worker verifies the indexer profile for scip-python 0.6.8, scip-typescript 0.6.0 and rust-analyzer 2026-11-02.
- **Full suites:**
  - backend unit 190 on macOS; on Linux the Rust, SCIP and engine tests (35) in the hardened image and 195 unit tests in the image;
  - integration 219.

0.5.28 adds public GitHub repositories (FR-028), run locally against a fake of GitHub's public endpoints that records every request:
- `tests/integration/test_public_repos.py` (4 tests):
  - A public repository builds to ready with no GitHub App configured. The tarball request carries no credentials while metadata calls use the operator token, and the repository is re-checked by numeric ID. Freshness and refs work, a second project shares the repository row, and push policies are refused.
  - Private (visible to the token), missing and malformed repositories are refused, and a foreign host never becomes a request.
  - A repository that turns private between the request and the download fails acquisition with `repository_not_public` and downloads nothing; a missing branch is `REF_NOT_FOUND`.
  - The App and the public source share one repository row, and a revoked App connection cannot be bypassed through the public source.
- `tests/unit/test_public_github.py`: strict `owner/name` parsing, answers about private or internal repositories refused even with a token, and rate limits reported as such.
- Full suites: backend unit 182 (macOS), integration 218, frontend 41.

0.5.27 records the T23 gate, "no network/install/hooks; timeout fallback labeled", run locally and in the backend image under the Compose hardening (read-only root, all capabilities dropped, no-new-privileges, `--network none`):
- **Indexer sandbox** (`tests/unit/test_scip.py`, Linux): the probe in the indexer profile opens no socket of any family and reads no parent environment, `/etc` or secrets. It cannot start Python or `/bin/sh` but can start Node, and writes only to the scratch `out`. Each profile runs only its own modules.
- **Real indexers:** methods resolve through their receiver (`g.greet` is `Greeter.greet`, not a module function of the same name; `new Box().open()` is `Box.open`), with no cross-language edges. A zero timeout falls back, and the scratch checkout is removed.
- **Mapping and decoding:** malformed indexes are refused. UTF-8, UTF-16 and UTF-32 columns map to scalars. Documents with line breaks tree-sitter does not count keep name matching, and locals are scoped per document.
- **Stage** (`tests/integration/test_builds.py`): resolved references are stored as `index` edges and shown on the card, the failed family keeps `name` edges, and the manifest counts both.
- **This repository** (195 files): name matching gives 16,501 edges, 8,243 ambiguous. The indexers give 5,194 edges with none ambiguous, in about 40–60 s.
- Full suites: backend unit 182 (Linux image) and 179 (macOS), integration 214, frontend 40.

0.5.26 adds per-project access control:
- `tests/integration/test_project_access.py` (3 tests): every project path is 404 for members without a grant, covering the project, builds, viewer data, freshness, push policy, variants, events, the members-only site and estimates.
- Admins and owners always see a restricted project. Grants work below a member's workspace role (an editor reading only) and above it (a viewer building). Revoking a grant and leaving the workspace take access away at once.
- Support access is excluded, and the row-policy backstop hides restricted projects from users but not from workers.
- The tenancy suite covers project grants, and the route sweep classifies the new routes.

0.5.25 records the M6 exit gates, one per feature, all run locally (PostgreSQL 18, SeaweedFS, replayed providers):
- **Push rebuilds (T21)** — `tests/integration/test_pushes.py`, 6 tests.
  - Gate: "repeated push schedules one appropriate build, never public auto-publish". A burst of three pushes becomes one build; a commit is never built twice.
  - Tags, deleted branches, other branches, repositories and installations are ignored. Duplicate deliveries count once.
  - Generated projects reuse their approved configuration within the push cap and wait for a person above it.
  - Work stops, and the policy turns off, when the policy's admin loses the role. No publication is ever created.
- **ZIP import** — a hostile-ZIP unit corpus plus `tests/integration/test_uploads.py`, 3 tests.
  - The corpus covers traversal, drive letters, backslashes, links, encryption, compression methods, an inflated-size bomb, entry count, duplicates and oversize files.
  - Upload, dedupe and scan work, and replay and generated builds run without GitHub. Request checks run before anything is stored.
  - Uploaded projects refuse the GitHub-only actions.
- **Variants (T22)** — `tests/integration/test_variants.py`, 3 tests.
  - Gate: "citation markers/schema and variant anchor tests pass". Slugs, order, source ranges and file IDs equal the base, and every citation is deterministically valid against the same snapshot.
  - The audience reaches only the writer, and the base plan is reused. Bad requests are refused. Unit tests cover the audience text and cache separation.
- **Own keys and batches (T24)** — `tests/integration/test_own_keys_batch.py`, 4 tests, plus an SDK-level batch adapter test.
  - Gate: "persisted batch IDs/keys, no silent price fallback". Keys are checked, sealed and shown only as hints. An own-key build spends on that key within its cap with the allowance untouched, and a removed key stops it.
  - A model without a batch price cannot be batched, and the high bound stays standard.
  - A batch persists its ID, waits without spending attempts, settles at the batch price, and falls back synchronously only for what it could not answer.
- **Isolation:** the tenancy suite covers push requests, provider keys and provider batches. The route sweep classifies every new route. Explicit grants still match a database migrated without defaults.
- **Frontend:** 36 Vitest tests, adding the push panel, the upload form, the variant switcher and the key panel. The E2E journey is unchanged and passes.
- **Deferred:** SCIP (T23), by decision (21); shipped in 0.5.27.

0.5.24 records the M5 exit checks (paid launch), run locally against a replayed provider (`FakeStripe` over `httpx.MockTransport`):
- **Payment failure matrix** (`tests/integration/test_billing.py`, 14 tests):
  - Webhooks: forged, stale, unsigned and tampered webhooks are refused and leave no receipt. Duplicates are acknowledged once.
  - A20: returning from checkout grants nothing until a verified event makes the server fetch the subscription.
  - Event order: an out-of-order deletion and a redelivered creation converge on the provider's state.
  - Overdue: a failed payment blocks generation (402) and recovers; past due beyond 7 days suspends hosting, and recovery lifts it but never an operator takedown.
  - Ending: cancel at period end keeps the plan; an ended subscription suspends hosting after 30 days only when plans are enforced.
  - Disputes are tracked by ID with the provider's current status. Two open disputes block until both close, and an early "closed" event changes nothing. A lost dispute blocks until an operator clears it. Refunds are audited without touching the ledger.
  - Reconciliation repairs a dropped webhook and reports a provider outage. An outage leaves receipts for the next cycle.
  - Metadata naming another workspace is rejected and audited.
  - The trial is once per workspace and per person, counted once, and never after a subscription.
  - Plan limits refuse new projects and seats when enforced. A subscribed workspace cannot be deleted. `GET /meta` is public.
- **Isolation:** the tenancy suite covers the new tables and functions: customers and subscriptions are tenant-scoped, the customer mapping is immutable, the resolver exposes no rows, and only owners start trials. The route sweep covers the four new workspace billing routes; the webhook and `GET /meta` are classified as unscoped. Explicit grants still match a database migrated without defaults.
- **Frontend:** 4 Vitest tests cover the waiting state after checkout, owner-only actions, the https-only redirect and the draft legal pages. The E2E journey (Playwright and axe, WCAG 2.1 A/AA) adds the start page with plans, Plan and usage before and after the trial, and the privacy, terms and support pages.
- **Security review of the M5 commit** found four issues, each fixed with a regression test that fails on the earlier code:
  - republishing lifted a billing suspension;
  - a newer abandoned checkout hid an ended plan;
  - each new workspace minted a trial;
  - closing one dispute cleared all of them, and a lost dispute unblocked generation.
- **Found while testing:** blank env values (`STRIPE_SECRET_KEY=` in the template) counted as configured credentials. They now mean unset, and a unit test covers it.
- **Accounting review and incident readiness:** spec 22 lists the records, the checks and the recovery commands.
- **Still open before charging customers (18):** the same matrix against Stripe test mode in staging; the legal review of the draft texts; owner email notices.

0.5.23 records the design pass:
- The Playwright journey passes with axe (WCAG 2.1 A/AA) on every screen in both light and dark themes. It found and fixed low-contrast line numbers.
- Unit tests cover the floating card: it opens at the name, pinning moves focus to its heading, and Esc returns focus to the name. The XSS corpus passes for the new viewer and the export viewer; the only SVGs are aria-hidden Lucide icons.
- Screenshots of the light and dark desktop project, build, code with hover and pinned cards, search, audit and account pages, and the 390 px phone reader, were reviewed during implementation (17's review artifacts).

0.5.22 fixes two defects found by building this project's own engine package in the local demo.
- Symbol keys collided across files: the index stage failed on `extract` defined in two modules. A regression test now builds two files that both define `main` and `Tool.run`.
- A stage whose writes fail no longer hangs leased. A regression test makes `apply` raise and expects `retry_wait` with no lease held.

0.5.21 records the M4 exit checks for SLOs, load and recovery. All were run locally on a laptop: one uvicorn process for the E2E server, PostgreSQL 18 and SeaweedFS in Compose.
- **Load (NFR-003, NFR-005):** a published replay site of 200 files with 20,000 definitions (20,200 search documents), with 50 readers for 60 s.
  - Run 1: read p50 30 / p95 131 ms (1724 requests, 0 errors), search p50 69 / p95 175 ms (568 requests, 0 errors), card p50 32 / p95 139 ms (563 requests, 0 errors); 47.6 requests/s. Passed.
  - Run 2: read p50 49 / p95 321 ms (1637 requests, 0 errors), search p50 99 / p95 376 ms (554 requests, 0 errors), card p50 48 / p95 361 ms (556 requests, 0 errors); 45.8 requests/s. Passed.
  - Targets: reads and cards p95 < 500 ms, search p95 < 700 ms, no errors.
  - Stress (no pauses, 50 concurrent loops): the single process saturates at 71.6 requests/s with p95 about 1616 ms and no errors. Capacity beyond that needs more API processes or replicas, which the shared rate limits and stateless API allow.
  - One reader (the probe's shape): read p50 21 / p95 49 ms (182 requests, 0 errors), search p50 56 / p95 89 ms (67 requests, 0 errors), card p50 22 / p95 52 ms (59 requests, 0 errors).
  - NFR-005's "cached card opens <100 ms" refers to a card already in the viewer's memory, which makes no request; server-side card fetches are held to the read target.
- **Restore drill (NFR-009, A22):** backup 277661 bytes in 0.54 s. Restore 2.28 s, reapply 2.71 s, purge 2.78 s, verify 2.97 s: RTO 10.73 s for this data set (target ≤ 4 h).
  - All checks passed: backup_predates_deletion, tombstone_reapplied, deleted_stays_deleted, no_source_resurrected, kept_site_consistent, rto_under_4h.
  - The first run failed: purge could not delete export and claim-check rows, because those privileges came only from the bootstrap's default privileges. 0020 makes every runtime grant explicit and removes four the defaults had added. A test now migrates a database without defaults and compares.
  - RPO ≤ 15 min depends on the managed database's continuous WAL archiving (PITR), which a local Compose stack does not have. It must be drilled on the staging database before paid launch (18).

0.5.20 records the threat tests for the M4 exit criterion "zero critical isolation/XSS issues":
- The isolation sweep covers 66 routes (27 unsafe). It finds no route that admits another tenant or anonymous visitors, leaks a victim ID or changes the victim's state.
- Mutation: admitting any non-member to read routes makes it fail with the leaking routes listed.
- The XSS corpus passes for the product viewer, the guide and the offline export viewer. An `innerHTML` mutation in the export viewer fails it.
- Tenant content responses are JSON with `nosniff` and `no-store`. 24 frontend tests pass.

0.5.19 fixes two review findings in the rate limiter. Hits run on their own pool. A unit test checks that IPv6 subjects collapse to their /64 and that mapped IPv4 is plain IPv4, also for the direct peer. The integration tests pass three runs in a row with distinct /64s per simulated client.

0.5.18 records shared rate limits (`test_rate_limits.py`, `tests/unit/test_ratelimit.py`):
- a user's sixth request in a minute at a limit of 5 gets 429 with a consistent `Retry-After`, while another user is unaffected;
- sign-in starts per address redirect to `/login?error=rate_limited`, while another address continues;
- public delivery per address returns 429 on the fourth request, while another address is served;
- build starts count new work and not idempotent replays; estimates and acquisitions count attempts;
- buckets hold no address; forwarded addresses are believed only from trusted networks (a spoofed header from an untrusted peer is ignored), and an invalid network setting is refused.

0.5.17 records platform operations (`test_operations.py`, 6 tests):
- Non-operators get 404 from every `/ops` route, and the database functions refuse them too (42501); the event helper is not callable by the runtime at all.
- Operators see aggregates and build diagnostics, with no paths or content. Without a support grant they get 404 on tenant data.
- The kill switch returns a retryable 503 on new builds while reads keep working; resuming restores builds, and both actions are logged.
- A takedown stops the public site, binds the admin (409 on unpublish), appears in the workspace's audit log with its reason, and can be reinstated.
- An entitlement raises the reported allowance by its amount and revocation restores it; both are audited, and 400 days is refused.
- Support access: editors can't grant it and 73 hours is refused. A grant admits the operator to reads but not writes (404 on build start and deletion), audits one session for several requests, and ends at expiry and at revocation. A non-operator gains nothing.

A tenancy test checks that the runtime role cannot insert an operator, read the operator tables, flip the platform switch, write an entitlement, or delete a tombstone or support grant. It holds whatever default privileges the database was bootstrapped with. The schema-wide guard found these grants while 0018 was being written.

Letting support access through write routes makes a test fail. Component tests cover the operations view (not-found for non-operators, the pause form) and the support panel; 21 frontend tests pass.

0.5.16 records T18 metrics (`test_telemetry.py`):
- with production logging, a replay build, a search, an export download and a forged sign-in leave no fixture secret, source text, query, file path, session cookie, CSRF token or UUID in the logs or the metrics exposition;
- the expected series exist with route templates; hostile label values become `other`;
- the gauges return the 11 aggregates to the system role only, with zero usage drift on the test data.

promtool validates the configuration and passes all 15 alert unit tests (`alerts.test.yml`). Raising the 5xx threshold makes a test fail. For the sweep race, a test holds a publish's `FOR SHARE` until the sweep's backend is waiting on the lock. The earlier single-statement guard purges the newly published build and fails that test; the fix passes. The first version of the test, which did not wait for the lock, passed under the mutation and was corrected.

0.5.15 completes T17 with retention sweeps (`test_retention.py`), checking each window at and past its boundary:
- exports expire, lose their object and download as 410;
- old unpublished builds lose rows and objects, while the published build and the newest builds stay; a purged build cannot be published or exported (409 `BUILD_EXPIRED`), and a second sweep changes nothing;
- failed builds are swept at 8 days but not at 6; events after 30 days, expired cache entries, and estimates no build uses;
- audit events at 181 days but not at 179, ended sessions after 30 days with the live session untouched, and expired idempotency keys;
- dispatched outbox rows at 8 days but not at 6.

Removing both published-build guards makes a test fail. A regression test covers the security fix: an operator takedown survives project deletion and purge, and its row cannot be deleted.

0.5.14 records T17, part of the M4 exit criterion. Integration tests (`test_deletion.py`) check:
- a project deletion: serving stops in the same transaction, with the project, build, export download and public site all 404;
- the running build ends cancelled with nothing reserved; the worker holding its lease fails its heartbeat and completion (LeaseLost), and a unit test shows that a lost lease cancels the running stage;
- the purge waits for `not_before`, then removes every content row and object while keeping the ledger's build rows, and frees the slug;
- a failed purge records its error and is retried after its lease;
- tombstone replay after a simulated restore (request and mark removed) deletes the project again with the same request ID and purges it, and re-purges a completed request when late objects reappear;
- workspace deletion (owner only, typed address) and account deletion (sign-in within 10 minutes, the sole-owner rule, a member who simply leaves, sessions and sign-in methods removed);
- tenancy: requests are visible only in their workspace or to their user, and the purger's claim returns identifiers only.

Removing the fencing call or the tombstone re-mark makes a test fail. The E2E journey gains the delete step: a member's private site becomes "Site not found" and the project and build APIs return 404.

0.5.13 records the M3 exit criterion "accessible full E2E". One Playwright journey runs in the installed Chrome against the real API with fake Google and GitHub, and aborts any request outside the app:
- sign-in, workspace, project, snapshot with the exclusion summary, estimate and a replay build followed over SSE;
- code with a pinned card and Esc, guide with claims, and review;
- a private publish read by an invited member, with another tenant and an anonymous reader shown "Site not found";
- public opt-in with the typed name, anonymous reading, and withdrawal;
- a ZIP export read offline with every network request blocked (none attempted).

axe (WCAG 2.1 A/AA) runs on twelve screens and found three real issues, now fixed: definition buttons whose visible line was not in their name (viewer and export), and links distinguished by colour alone. It passes three runs in a row. Project deletion is not yet in the journey (M4).

0.5.12 adds freshness and the audit log.
- Freshness: integration tests move through never-built, current, a cached observation, update-available after the branch moves, and `unknown` with the last observation when GitHub is unreachable (A23). Strangers get 404.
- Audit: the log is newest first, paginates with filter-bound cursors, filters by action prefix, and is admin-only (403 for editors, 404 for strangers).

289 backend and 17 frontend tests pass.

0.5.11 records T16, the remaining part of the M3 exit criterion. Integration tests open the ZIP and check:
- exactly the fixed entries, with no excluded or blocked content anywhere (A19, A07);
- no network references, every referenced asset present, and manifest hashes that match;
- a KB that validates against its contract, and byte-identical archives across exports.

Further tests cover the KB format, requester-or-admin downloads with the editor role required to export, a 410 after expiry, audit events, the ready-build precondition and the hourly quota. Exports never change the build. Opening the archive in a network-blocked browser is part of the E2E step. 287 backend and 17 frontend tests pass.

0.5.10: unpublishing an operator-suspended publication is refused by the API (409) and by the database trigger (IntegrityError). Removing the service check makes the test fail.

0.5.9 hardens publication.
- A suspended workspace or deleted project stops delivery, and reactivation restores it.
- An operator suspension survives republishing.
- In a two-connection race, the publish waits for an uncommitted revoke and then refuses.

Removing FOR SHARE or the operator check makes a test fail. 9 publication tests pass.

0.5.8 records T15, part of the M3 exit criterion (public opt-in and revocation). Integration tests cover:
- private-by-default publication (A15), public only with an address and the typed project name, and anonymous reading of tree, source, guide, claims and search without the build ID ever appearing;
- ETag preconditions (428, 409), rollback to an older build, and unpublishing that stops delivery at once;
- the review gate (unchecked claims block), the acknowledgment for partly supported claims, admin-only publishing, a foreign build refused, and a stranger's 404;
- a revoked connection or repository that suspends delivery at once and blocks republishing (A17, FR-027);
- a members-only private site address.

Mutations are caught for: the confirmation check, the gate, the ETag comparison, the private-site membership check, the access check and the suspension trigger. Frontend tests cover the publish form's confirmation and acknowledgment rules, the admin-only controls and a withdrawn site. 279 backend and 15 frontend tests pass.

0.5.7 records T14. Search integration tests cover:
- exact-name, prefix and docstring-text hits in that order;
- kind filters, and inert LIKE and tsquery characters;
- excluded secrets that are never searchable (A07), and 404 for other tenants (A04).

A schema-wide tenancy test requires FORCE RLS and a runtime tenant policy on every table with `workspace_id`, so new tables cannot ship without them. Frontend tests cover the build tabs, deep-linked search results, the `/` shortcut, Review with admin-only waivers, and the pinned definition card taking focus, closing on Esc and returning focus. 273 backend and 12 frontend tests pass.

0.5.6 fixes the stream-slot leak: a test runs more aborted responses than the session limit allows, and every slot comes back; removing the fix makes it fail.

0.5.5 adds the SSE build stream. Integration tests cover:
- history replay and resume from Last-Event-ID;
- a running build followed to `build.ready`;
- heartbeats on an idle stream, and a revoked session ending it with `access.revoked` (A17);
- 404 for strangers, and the per-session and per-workspace stream limits.

The frontend follows builds with EventSource and falls back to polling every 5 s with jitter. 268 backend and 10 frontend tests pass.

0.5.4 adds the M2 evaluation baseline. On the corpus, the replay run passes every hard gate:
- scan as labeled;
- 50 of 50 labeled definitions matched (precision and recall 1.0) and 100% span validity;
- the malformed file reported as a parse error;
- 9 of 9 explanation answers valid, none containing injected text, and every prompt with intact data blocks;
- 4 of 4 invalid citations rejected with the labeled reason.

On 34 labeled claims, the replay checker's supported-label precision is 0.54 (Wilson 95% 0.35–0.71), and recall 0.67 (0.45–0.83). That measures a heuristic and is the reason a preset may no longer pair a model writer with it. The labels are drafts by the implementing assistant, and the report marks the baseline provisional until two maintainers label it; no accuracy claim follows from it. A model-checker baseline needs an operator catalog, an API key and approval to spend. CI pins the baseline report and checks that it reproduces byte for byte. 263 backend tests pass.

0.5.3 records cancellation, retry and event polling. Integration tests cover:
- a queued build cancelled at once with its full envelope released;
- a cancel during a call: in-flight calls settle, no later stage runs, and the ledger balances (A10);
- with one call in flight, the next unit refused at its earmark, so exactly one call happens;
- a retry after a checker outage that pays only for checks and becomes publishable (A13);
- retry rules: not while running, not with another snapshot's estimate, and 404 for strangers;
- ordered, resumable events.

Removing the cancel check at the earmark, or the cancel at the stage boundary, is caught. The frontend adds the estimate flow (snapshot, scope, preset, cost range, price revision, expiry, cap), cancel and retry, usage, and the guide with claim badges. Guide text renders as text, and a test confirms that an HTML-looking model output creates no element. 259 backend and 10 frontend tests pass.

0.5.2 records T12. Integration tests cover:
- a replay guide whose claims are all supported, so the build is publishable;
- a checker outage that leaves every claim unchecked on a ready draft and blocks publication (A13);
- a repair cycle that removes unsupported claims, keeps the original page and rows, and re-checks only changed claims;
- unsupported claims that block until an admin waives them with an audited reason, then stay counted;
- a citation by path instead of file ID that is invalid, never sent to the checker, and refused for waiver by the API and by a database CHECK (A14);
- quality endpoints that are private to the workspace.

Unit tests cover extraction (trailing citations, list items, code fences, slug-bound keys), each deterministic rejection reason, checker and repair validation, and the gate. Seven mutations are caught: citing excluded files, skipping range bounds, treating an outage as supported, waiving anything, letting unchecked claims through, sending invalid claims to the checker, and keeping superseded rows current. 253 backend tests pass.

0.5.1 fixes two findings of the automated review of T11. With the scope narrowed to `src/app.py`, no captured prompt contains `src/util.py`, its `helper` definition, or the excluded secrets; removing the new filter makes that test fail. New allowlist cases reject reference-style and autolink `javascript:` targets and raw HTML in list items and blocks. 232 backend tests pass.

0.5.0 records T11, the provider gateway and generation stages. Integration tests run the real pipeline with the deterministic replay gateway, and a scripted fake stands in for a priced provider; no real provider is called. They cover:
- a free replay-preset build through explain, overview, plan, guide and render, with labeled cards, file explanations, cited guide pages, a manifest listing the guide, and the guide endpoints private to the workspace;
- priced calls whose actual cost never exceeds the earmark, with a ledger that balances (reserve, settle, release) and provider request IDs kept;
- budget exhaustion before any request;
- a rate-limited call retried without charge, and an invalid answer billed and then repaired;
- a refusal falling back to a labeled source-only card;
- an unknown outcome keeping its hold after the build ends;
- a crash after paid output resuming from the checkpoint without paying again (A09);
- a fenced-out worker settling and checkpointing nothing;
- a killed worker's attempts becoming `unknown` on re-claim, with their holds kept.

Unit tests cover chunking, data-boundary markers, validators, the Markdown allowlist, the schema projection, and the Anthropic adapter against a mock transport, including error normalization and a request with no sampling parameters or tools. Mutation checks catch removing the earmark budget check, the settlement fence, unknown-on-re-claim, cache reuse, the repair call, symbol-key validation and the raw-HTML check. Cancellation has no test until T13. 224 backend tests pass (one Linux-only test skipped on macOS).

0.4.2 bounds scope selection. Against 2000 files with 29 segments of 200 characters each and 40 hostile but valid globs, every selection finishes or stops at the budget within 1.6 s (previously 61 s); ordinary globs take 0.2 s on those paths and 37 ms on a realistic repository. 183 backend tests pass.

0.4.1 fixes two findings of the automated review of T10. A probe of `PurePath.full_match` with `*a*a*a*a*a*a*a*a*a*a*b` against a 960-character path did not finish in two minutes; the new matcher agrees with it on ordinary globs and handles the hostile ones in milliseconds (unit tests). An integration test shows a second project's acquisition refused while the first is pending, and admitted after it finishes. 180 backend tests pass (one Linux-only test skipped on macOS).

0.4.0 records T10 (M2): snapshots before builds, estimates and single-envelope reservations. Integration tests on PostgreSQL 18 cover:
- a snapshot acquired and scanned before any build, with signed, filter-bound file cursors;
- an immutable estimate that expires, goes stale when the catalog changes, and fixes the approved commit even after the branch moves (A05);
- a cap reserved once and released at the end, with ledger entries balancing the account counters;
- a cap above the remaining allowance refused, and the workspace active-build limit;
- a two-session race in which the second reservation waits for the account lock (A08);
- other tenants seeing 404 for estimates, snapshots and usage.

Mutation checks catch removing the account row lock, the allowance check, the expiry check and the terminal release. An API-level race alone did not catch the missing lock, because the two requests ran one after the other; the direct two-session test does. 176 backend tests pass.

0.3.12 hardens the sandbox. The Linux probe ran with a world-readable `/run/secrets/github_app_key` present. Inside the sandbox it could not read the parent's environ, `/etc` or the secret, and could not exec or open any socket; the parser still works under Landlock. On the Compose stack, the hardened worker passed the stricter self-check, and its own environ is unreadable to same-UID processes (`PR_SET_DUMPABLE=0`). 146 backend tests pass.

0.3.11 records the M1 implementation. M1 exit criteria are met by these tests:
- **End-to-end build:** an immutable fixture build runs from a GitHub ref through acquire, index, explain and render to the private viewer, with secrets excluded or blocked, Unicode columns, name-based references and a manifest stored last.
- **Cross-tenant isolation:** tenancy, flat-route 404s and the viewer are private.
- **Crash and retry:** duplicate notifications claim once; a lost lease is fenced and reconciled; transient failures retry, then fail the build; cancelled builds are not claimed; lost messages are re-notified; dispatch backs off on broker failure.
- **Parser network isolation:** on Linux, inside the sandbox, IPv4, IPv6 and Unix socket creation are refused, and the environment is empty.

145 backend tests pass on PostgreSQL 18 (one Linux-only test runs in a Linux container and CI), plus 8 frontend tests. Mutation checks were caught for completion fencing, live-lease re-claim, cancelled-build claims, and every earlier guard. On the running Compose stack:
- the hardened worker passed its sandbox self-check;
- tasks flowed outbox, dispatcher, RabbitMQ, worker;
- a poison message was refused and kept in `codehover.dead`.

0.3.10: migration 0004 fails closed. A migration test creates a repository at 0003, upgrades, and asserts that it is revoked; the test fails if the fail-closed step is removed. 96 backend tests pass.

0.3.9 limits GitHub connections to the connector's repository grant (09). Two integration tests cover a member who can read one of three repositories, and a narrower reconnect revoking the rest. Two mutations, granting the whole installation at connect and listing without the grant, are each caught by both tests. 95 backend tests pass, and migration 0004 runs in the Compose `migrate` job.

0.3.8 records the T05 GitHub App flow. The implementation passes 10 GitHub integration tests on PostgreSQL 18 against a fake GitHub. The fake checks PKCE, the App JWT signature and issuer, and per-installation repository access. The tests cover spoofed and other users' installations, a different GitHub account, a Google-only user, phase confusion, other-browser and other-session hijacking, mid-flow demotion, a repository removed from the installation, and a revoked connection. Five code mutations of the guards are each caught. 93 backend tests pass.

0.3.7 records T04 workspace membership. The implementation (migration 0003) passes 10 workspace integration tests on PostgreSQL 18, part of 83 backend tests. They cover the role matrix, invitations with verified-email matching, single use, revocation and expiry, non-member 404s, ETag preconditions, a signed and scoped pagination cursor, and a true two-connection race on the last two owners, of which exactly one commits. Mutation checks:
- The admin role bound, the invitation email check and the member-list workspace filter are each caught.
- Dropping the owner trigger is caught by the race test.
- Removing the application's last-owner check is equivalent, because the trigger still refuses with `LAST_OWNER`.

0.3.6 binds OAuth flows to the starting browser (09). The login-CSRF integration test opens a callback in a browser without the flow cookie, then with a forged cookie, and confirms that both are refused and that the originating browser can still complete. A schema mutation that ignores the binding is caught by two tests. Removing the application's missing-cookie check is an equivalent change, because the database guard still refuses; the check stays as a second layer. 73 backend tests pass.

0.3.5 records sign-in decisions implemented in T03. They are exercised by 26 sign-in integration tests on PostgreSQL 18 with mocked Google and GitHub providers, which sign real RS256 ID tokens and check PKCE. Nine code mutations of security checks were applied; eight were caught. The ninth, dropping the explicit RS256 allowlist, is equivalent, because joserfc's defaults already reject `alg: none` and HS256 algorithm-confusion tokens; the allowlist stays as defense in depth. The Docker gateway was also checked: signed-out 401 with `no-store`, and unconfigured providers redirect to the login page.

0.3.4 adds `idempotency_keys` and the pre-authentication lookups to 05. Both are implemented by application migration 0002, which was:
- executed on PostgreSQL 18 (upgrade, downgrade and re-upgrade, also inside the Compose `migrate` job);
- covered by 46 backend tests, including 21 integration tests for tenancy, identity, sessions, OAuth state, append-only tables, the system role and integrity rules;
- mutation-tested: six injected privilege or policy defects were each caught.

0.3.3 adds the `SPEC_VERSION` conformance link (23). Both images were rebuilt with the new `SPEC_VERSION` build argument; both carry `codehover.spec.version=0.3.3` next to `org.opencontainers.image.version=0.1.0`, and the stack restarted ready. `scripts/check_versions.py` rejects an unknown, non-SemVer or newer-than-package `SPEC_VERSION`, and reports, without failing, an implementation behind the spec. Everything below was executed for 0.3.2 and is unaffected.

0.3.2 is the first version whose container templates (`deployment/`) were executed. They ran against the M0 implementation in the repository root. The steps were:
1. Built both images.
2. Started the full Compose stack.
3. Exercised it through the gateway and from a real browser (details below).

The SQL contracts are unchanged since 0.2.0, and the versioning tooling since 0.3.0. Earlier labels were renumbered: "1.0" is 0.1.0, and "1.1" (briefly "2.0.0") is 0.2.0.

## Container templates executed (0.3.2)

Docker 29.8.2, Compose 5.5.1, linux/x86_64, `--profile app`:
- **Images.**
  - Both images build from the hash-pinned Python lock and the npm lockfile.
  - The frontend bundle is byte-identical to a local build.
  - The backend image contains no tests, `.env` files or keys; its code is root-owned and not writable by the runtime user (uid 10001); its OCI version label is 0.1.0.
- **Start-up order.**
  1. `db`, `rabbitmq` and `objectstore` (SeaweedFS 4.52, pinned by digest).
  2. `migrate`, which exits 0.
  3. `bootstrap`, which exits 0.
  4. `api` (healthy), the worker shells, and `web`.

  `/api/v1/health/ready` is ready through the gateway. Re-running `migrate` and `bootstrap` is a no-op.
- **Gateway.**
  - `/`, deep links, hashed `/assets/` and `/api/` responses all carry nosniff, Referrer-Policy, X-Frame-Options and the CSP. The assets location fix from 0.2.0 was confirmed.
  - The SPA shell is `no-cache` and assets are `immutable`.
  - nginx's request ID reaches API responses.
- **Browser** (Chrome). The app renders and fetches `/api/v1/health/live` with no console errors or CSP violations. Unknown routes render the in-app not-found view.
- **Object store.** Bucket `codehover` exists. Anonymous list, read and PUT return 403.
- **Broker.** The durable topic exchange `codehover.dlx` exists. Policy `codehover-work-dlx` applies to `^codehover\.work\.` quorum queues with the DLX and `delivery-limit` 20.
- **Database roles.**
  - The API connects as `app_runtime`.
  - `app_runtime`, `app_system` and `app_resolver` exist, none superuser or BYPASSRLS.
  - The application's integration tests pass against the container database: migration down/up cycle, readiness, and role limits.
- **Shutdown.** The worker, dispatcher and scheduler exit 0 on SIGTERM.
- **Found and fixed during the run.**
  - The default host port 5432 collided with a local PostgreSQL; the host-port overrides were added.
  - A bootstrap log field used a reserved `LogRecord` key and crashed the job; it was fixed in the application, with a regression test.

## Versioning checks (0.3.0)

`tools/assemble.py --check` now also verifies:
- `VERSION` is valid SemVer 2.0.0 and matches the newest changelog release;
- the changelog has an Unreleased section, unique releases, and descending SemVer precedence and dates;
- README, master spec and manifest carry the stamped version;
- every `contracts/*.schema.json` has a SemVer `$id` URN whose MAJOR matches its title and any `schema_version` const.

The following were exercised on a scratch copy:
- `bump minor`;
- refusal to bump an empty Unreleased section;
- rejection of a non-SemVer `VERSION`;
- detection of an out-of-order changelog;
- detection of an `$id`/`schema_version` mismatch.

SemVer precedence sorting was checked against the semver.org example ordering.

## Executed in 0.2.0

- **SQL contracts on PostgreSQL 18.0** (local throwaway cluster). Steps:
  1. Ran `deployment/init-db.sh` with the compose environment variables.
  2. Ran `contracts/core_schema.sql` as the bootstrap/DDL user.
  3. Ran `contracts/core_schema_checks.sql`.

  All checks C01–C09 passed:
  - UUIDv7 defaults;
  - no rows without a tenant context;
  - tenant isolation and rejected cross-tenant writes;
  - user-scoped workspace listing;
  - the resolver functions;
  - atomic workspace creation;
  - the append-only ledger;
  - the `app_system` scope, which can claim due work across tenants but cannot read tenant tables or task outputs;
  - one active build per project, same-project publication FK, public-slug CHECK and budget cap CHECK.
- **Mutation test of the checks.** Six deliberate defects were each applied inside a rolled-back transaction, and each was caught by its check:
  - a permissive runtime policy;
  - a system role able to read projects;
  - an UPDATE grant on the ledger;
  - the dropped one-active-build index;
  - the dropped user-membership policy;
  - the dropped public-slug CHECK.
- **`python3 tools/assemble.py --check`**:
  - every JSON file parses and every `*.schema.json` is a valid Draft 2020-12 schema;
  - YAML parses, including Compose merge keys and service dependencies;
  - shell syntax passes;
  - Markdown code fences are balanced and there are no TODO/TBD markers;
  - the master spec matches its sources;
  - the manifest checksums match.
- **Explanation schema v2.** A sample document validates.
- **Design tokens.** WCAG contrast ratios were computed for every text token and the new `input-border` token (results in 17).

## Corrected from 0.1.0

The 0.1.0 report listed "document checksum consistency" as passed, but its manifest was stale for 7 files and the master spec. The master spec itself matched its sources. Checksums are now generated by `tools/assemble.py` rather than maintained by hand.

## Not performed

- `nginx -t` as a separate step (the config was loaded and exercised by the running gateway instead). Multi-node RabbitMQ, managed object storage and production TLS ingress.
- SQL beyond the core subset and the M0 baseline migration.
- Live OAuth, GitHub App installation, provider calls and billing.
- Accessibility audits, performance, security penetration tests, and end-to-end tests beyond the M0 shell.
- The GitHub App installation-verification flow (09) relies on GitHub's documented `GET /user/installations` behavior and must be confirmed against a real App in M1.

The core SQL is a subset design reference. The API specification is a design contract, not generated OpenAPI. Container templates require the future application files and exact dependency lockfiles. Production image tags and digests, and the model/provider configuration, remain implementation gates.
