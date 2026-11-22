# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

M0 to M6 are implemented, including SCIP (T23), plus per-project access control.

- **M0–M1:** sign-in, workspaces, GitHub App connection with per-repository grants, snapshot acquisition with a safe scan, the outbox/RabbitMQ job system with fenced leases, a sandboxed tree-sitter parser, and a private viewer.
- **M2:** the model catalog, estimates, single-envelope budget reservations, and the provider gateway. The Anthropic SDK and a deterministic replay gateway sit behind it. It adds explanation, overview, plan and guide stages, deterministic citation checks, the independent checker with one repair cycle, the quality gate and waivers, cancel and retry, and a labeled eval harness (`backend/evals/`).
- **M3:** the build viewer with Overview, Code, Guide, Search and Review tabs, using a product-controlled line renderer rather than CodeMirror (an open decision). It adds the build SSE stream (`app/modules/projects/events.py`) and full-text search over `search_documents`. Publication (`publication.py`) covers private by default, a public opt-in with typed confirmation, ETags, rollback and suspension triggers. Published sites are served at `/published/{slug}`. Exports are a ZIP or KB JSON produced by the `export` stage, served to the requester or an admin and expiring after 24 h. M3 also adds project freshness, the admin audit log, and the Playwright + axe journey (`frontend/e2e/`, `backend/tests/e2e/server.py`).
- **M4:**
  - Deletion of projects, workspaces and accounts with tombstones (`deletion_requests`, plus copies under `tombstones/` in object storage), fencing and purge (`app/jobs/purge.py`).
  - Retention sweeps (`app/jobs/retention.py`) and tombstone replay after restore (`app.ops.tombstones`).
  - Prometheus metrics on an internal port (`app/core/metrics.py`, `deployment/prometheus/` with tested alerts).
  - Operations: an operator role, the `/ops` view, admin-granted read-only support access, the kill switch, takedowns and manual entitlements (`app/modules/ops`, `app.ops.operators`).
  - Rate limits shared across replicas (`app/core/ratelimit.py`).
  - Threat tests (`tests/integration/test_threats.py`, `frontend/src/features/viewer/xss.test.tsx`), the load check (`app.ops.loadcheck`) and the restore drill (`tests.e2e.restore_drill`).
- **M5:**
  - Stripe through a small httpx adapter (`app/adapters/billing/stripe.py`, not the SDK): Checkout, the Customer Portal and signature verification. `app/modules/billing` holds plans, state, sync and the router.
  - Billing state is the reconciled `subscriptions` table (migration 0022). It is written only from the provider's answer: the worker processes `webhook_receipts` and re-fetches the customer's subscriptions. Reconciliation runs daily and through `app.ops.billing`. A checkout redirect grants nothing (A20).
  - The plan lifecycle: past due or disputed blocks generation (402); long past due or ended suspends hosting with reason `billing`; there is a one-time trial (`app_private.start_trial`, once per workspace and per person); disputes are tracked by ID, and a lost one blocks generation until an operator clears it; plan limits apply, with `BILLING_ENFORCED` for projects and seats.
  - The Plan and usage page, plans on the start page, draft privacy/terms/support pages (`GET /meta`), the billing runbook and the accounting review (spec 22).
  - The payment failure matrix: `tests/integration/test_billing.py`, with `FakeStripe` over `httpx.MockTransport`.
- **M6** (P1, each with its own exit gate):
  - Push rebuilds (`projects/pushes.py`, 0024): a per-project push policy (off, snapshot or build) with a push cap, debounced `push_requests`, and work on behalf of the policy's admin. Nothing a push starts is ever published.
  - ZIP import (`projects/uploads.py`, `acquire.extract_zip`, 0025): one `upload` connection per workspace; each content-addressed snapshot is stored under `…/snapshots/{sid}/upload.zip`.
  - Language and level variants (prompt set 0.4.0 with the audience text): `variant_of` a ready base reuses its snapshot, scope and guide plan, so anchors match. Several languages in one estimate (up to three) start the base and a variant per further language together (`service.start_generation_build`, `_start_one`; the server sets `config.family` on those variants, which count as one active build and run beside the base, 0032); a family variant's plan stage waits for the base's plan (`generation._await_base_plan`). With `translate` (prompt set 0.6.0, P07), further languages translate the base's validated text instead: each of explain, overview and guide waits for the base's stage (`generation._await_base_stage`), translations must keep the source's structure (`generate.translation_problems`), and estimates price them separately (`per_translation`, proportional cap shares). A publish records the variants it approves (`publication.approved_variants`, `publications.variant_build_ids`, 0033); sites serve them by `language` (`published/router.site_build`), and static ZIPs carry the other languages (`exports.family_variants`).
  - BYOK and batch (`providers/keys.py`, `jobs/batching.py`, 0026): sealed workspace Anthropic keys and `funding: own_key` builds without the allowance; explain batches at the catalog batch price, with persisted batch IDs and `StageWait`.
  - SCIP (T23; `engine/indexers.py`, `index_worker.py`, `scip.py`): scip-python, scip-typescript and rust-analyzer run in the sandbox's `indexer` profile, one program per run; resolved references are `index` edges, and a failed or timed-out family keeps labeled `name` edges.
  - Rust: tree-sitter-rust in the parser; rust-analyzer reads a `rust-project.json` that the sandboxed worker builds from the Cargo manifests (`engine/rust_project.py`), so cargo, build scripts and proc macros never run. It is installed by `backend/indexers/fetch_rust.py` (pinned by SHA-256) together with Rust's library sources.
- **C and C++** (parser 0.5.0, `parse_worker.py` `_c_definition`): definitions through declarators, prototypes merged into definitions, comment docstrings; `.h` is C++ when the snapshot has C++ sources (`scan.scan`). Uses resolve by name only.
- **Public GitHub repositories** (0028, `projects/public_repos.py`, `adapters/github/public.py`): a `public` connection kind reads public repositories without the App; visibility is re-checked on every resolve and download, tarballs come anonymously from codeload, and the optional `GITHUB_PUBLIC_TOKEN` only raises the metadata rate limit. No push rebuilds. A public site of one needs a permitted license at the build's commit, with the license file in the build (FR-029, 0029, `projects/licenses.py`); private sites need none.
- **More model providers** (0030, `adapters/models/{openai,gemini,openrouter}.py`, shared `_http.py`): OpenAI (Responses API), Gemini (Gemini API `generateContent`) and OpenRouter (chat completions, `max_price` = catalog price, no `models` fallback) over plain httpx, for operator keys (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, optional `OPENAI_BASE_URL`) and workspaces' own keys. Batches run for Anthropic, OpenAI (JSONL file, `/v1/responses`) and Gemini (inline `batchGenerateContent`); the catalog refuses an OpenRouter `batch_price`.
- **codehover's design** (00 "Reuse decision", MIT notice at `frontend/public/licenses/codehover.txt`): the reading view (`frontend/src/styles/viewer.css`, `.reading`) has codehover's palette, kind colours and type; the code view shows line notes (gutter mark, on hover or `notes=inline`) and opens cards at every use of a definition (`references` in file views) and documented parameter (`codeLine.tsx`); guides have sections (guide-plan 2.0.0, 0031), tables, `guide:<slug>` links, Mermaid drawn as SVG images, definitions named in backticks linked (`symbols` on guide pages) and a landing view. The offline ZIP ships the same reading view (`frontend/export.html`, `src/export/`: `ExportApp` over `staticSource(kb)`), built into `frontend/dist-export` and copied by `exports.static_zip`. Prompt set 0.5.0 carries codehover's writing guidance; 0.6.0 adds the translation template.
- **Per-project access** (0027, `projects/access.py`): a project is open to the workspace or restricted; restricted projects are visible to admins, owners and members with a viewer or editor grant, and the grant is their role there.
- **Hosted MCP** (0035, `app/modules/mcp`): `POST /api/v1/mcp/{slug}` answers MCP JSON-RPC (Streamable HTTP, stateless, JSON responses) with codehover's eight read-only tools over the site's build (`tools.py`, through the viewer's service functions; `language` picks an approved variant). Public sites are anonymous; private ones take `Authorization: Bearer chm_…`, a personal access token (`tokens.py`; digest only, `app_private.access_token`) of a member who may see the project. Cookies are never read there. Tokens are managed at `/workspaces/{wid}/access-tokens` (workspace page) and revoked when the member leaves.
- **Owner notices by email** (0034, `app/modules/notices`, `adapters/email`): `owner_notices` rows are recorded in the changing transaction (billing sync: past due, dispute opened/lost, via `notices.record` with a dedupe key; every site suspension via the statement-level `publications_notice_suspension` trigger), and the worker's maintenance loop sends them to active owners and admins over SMTP (`delivery.deliver_due`: claim, send with no transaction open, record if the claim still holds). Compose's `mail` service (Mailpit, inbox http://127.0.0.1:8025) is the worker's default relay; `SMTP_HOST=` turns email off.

The eval labels are drafts until two maintainers label them. Live use needs real Google/GitHub OAuth and GitHub App credentials, plus an operator model catalog and provider key for paid presets. The specification lives in `build-package/`. codehover-cloud is a planned multi-tenant SaaS that turns a GitHub repository into a browsable, cited explanation website, with hover cards, reference graphs and architecture guides. It is a hosted version of the `cahya-wirawan/codehover` CLI. Its engine is written independently: reusing any reference code needs an explicit decision and a license review. The one decision so far (2026-11-21, `00_REFERENCE_ANALYSIS.md`) reuses codehover's viewer design and prompt writing guidance under MIT, with the notice at `frontend/public/licenses/codehover.txt`.

The numbered documents in `build-package/` are normative requirements. `CODEHOVER_WEB_MASTER_SPEC.md` is generated from them, so edit the numbered files, then regenerate it (see Commands). Record every package change under `## [Unreleased]` in `build-package/CHANGELOG.md`. `build-package/VERSION` is the only hand-edited version. Choose the bump from the package rules in `23_VERSIONING_AND_RELEASES.md`, which also defines versioning for the product itself: API path major, contract `$id` URNs, engine and prompt-set SemVer, presets and catalogs. The package is in initial development (0.y.z) until an M1 implementation conforms. Until then, an incompatible contract change (a removed or renamed field, endpoint or setting, a new required setting, or a stricter schema) bumps MINOR, and additive or editorial changes bump PATCH. Never bump to 1.0.0 without the user's say-so. Reading order: `01_PRD` → `04_SYSTEM_ARCHITECTURE` → `05_DATA_MODEL` → `06_API_SPEC` → `07_AI_OR_AUTOMATION_PIPELINE` → `15_PRODUCT_BACKLOG`. For UX, read 02, 03 and 17. For ops, read 22 and `deployment/`. The machine-readable contracts are in `build-package/contracts/`: `core_schema.sql` is a DDL subset showing tenancy, RLS and outbox patterns, not a complete migration; the folder also holds the JSON Schemas for explanations, overview, guide plan and page, claim checks and repairs, and task envelopes. Prompts live in `19_PROMPT_LIBRARY.md`.

Next scope: nothing is scheduled after M6 (`14_IMPLEMENTATION_ROADMAP.md`); candidates are P2 custom domains (T25; hosted MCP is done). Known gaps:
- event-stream concurrency limits are still per process;
- RPO ≤ 15 min needs a PITR drill on the managed staging database (the local drill covers the restore procedure and RTO only);
- the load numbers come from a single local process, not staging;
- the payment failure matrix has run only against the replayed provider, not Stripe test mode in staging;
- the legal texts are drafts awaiting jurisdiction review;
- owner notices have run only against the recording fake and the local Mailpit, not a production relay;
- only the explain stage is batched (Anthropic, OpenAI and Gemini); the OpenAI and Gemini batch paths have run only against mocked HTTP, and Gemini's batch state and result fields are read in every place its docs give them;
- the OpenAI, Gemini (`gemini-3.8-flash`, `generateContent` with `responseJsonSchema`) and OpenRouter (`qwen/qwen4-max`) adapters have run live only through the eval harness (2026-11-20), not a full build; OpenAI's input counts include cached tokens (shown by repeated runs), OpenRouter's is assumed to follow the same convention;
- SCIP links calls through an interface or Protocol to the interface method, and never reads a repository's tsconfig path aliases;
- C and C++ uses resolve by name only (no indexer; scip-clang would need a compile database), and macros other than function-like `#define`s are not definitions;
- Rust resolution has no crates.io dependencies, proc-macro expansion or build-script output, and assumes a 64-bit Linux target;
- a published site and an offline ZIP switch languages only at the published or exported build's reader level;
- the permitted-license list awaits legal review, and GitHub's single-license answer refuses some multi-license or `NOASSERTION` repositories (spec 21);
- hosted MCP has no server-initiated stream, resources or prompts, and authenticates only with a pasted token (no MCP OAuth flow);
- rotating `APP_ENCRYPTION_KEY` makes stored provider keys unreadable.

Without a plan, a workspace gets `GENERATION_ALLOWANCE_MICRO_USD`; a held plan replaces it with the plan's allowance, and manual grants add to either.

## Stack

- **Backend:** Python 3.13, FastAPI, Pydantic 2, SQLAlchemy 2.x async, psycopg 3, Alembic, aio-pika (explicit consumers, no Celery), httpx, Authlib.
- **Data:** PostgreSQL 18 with native `uuidv7()` PKs (no UUID extension). RabbitMQ 4. S3-compatible private storage (SeaweedFS locally, pinned by digest). No Redis.
- **Frontend:** React, TypeScript, Vite, Tailwind, shadcn/ui, TanStack Query, React Router, React Hook Form with Zod, and a read-only line renderer for code (CodeMirror 6 is an open decision).
- **Tests:** pytest and pytest-asyncio against real PG18 and RabbitMQ containers; Vitest with RTL; Playwright with axe.

Layout (`16_REPO_STRUCTURE.md`; modules, engine and adapters arrive from M1): `backend/app/{core,modules/<domain>,workers,engine/<stage>,adapters/<integration>}`, `backend/alembic/`, `backend/tests/{unit,integration,security,fixtures}`, `frontend/src/{app,features,components,lib}`, `frontend/e2e/`, `deployment/`, `contracts/`.

## Commands

Package maintenance (from `build-package/`; needs PyYAML and jsonschema):

```bash
python3 tools/assemble.py              # regenerate master spec, manifest checksums and version stamps after editing
python3 tools/assemble.py --check      # CI-style check: JSON/schema/YAML/shell/fences/staleness/version consistency
python3 tools/assemble.py bump minor   # release CHANGELOG "Unreleased" entries as the next SemVer version
# SQL contracts against a PostgreSQL 18 database (bootstrap user = DDL role):
POSTGRES_USER=codehover_ddl POSTGRES_DB=codehover APP_DB_PASSWORD=x APP_SYSTEM_DB_PASSWORD=y sh deployment/init-db.sh
psql -v ON_ERROR_STOP=1 -d codehover -f contracts/core_schema.sql -f contracts/core_schema_checks.sql
```

Application (details and single-test examples in `README.md`):

```bash
cd backend && uv venv --python 3.13 .venv && uv pip sync --require-hashes requirements-dev.lock
.venv/bin/ruff format . && .venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest -m "not integration"
.venv/bin/pytest -m integration          # needs TEST_MIGRATION_DATABASE_URL / TEST_DATABASE_URL on a PG18 DB bootstrapped by deployment/init-db.sh; refuses to run while stack workers/dispatcher/scheduler are connected to that DB
.venv/bin/python -m app.openapi_export   # regenerate contracts/openapi.json after API changes (CI checks drift)
cd frontend && npm ci && npm run lint && npm run typecheck && npm test && npm run build && npm run build:export && npm run check:env
# build:export: the offline ZIP's reader (export.html -> dist-export/), which static exports copy; set EXPORT_VIEWER_DIR elsewhere
cd frontend && npm run test:e2e           # Playwright + axe journey; same TEST_* URLs as integration, installed Chrome
.venv/bin/python -m app.ops.operators grant <user-id> "<note>"   # operations role (DDL role: MIGRATION_DATABASE_URL)
.venv/bin/python -m app.ops.tombstones reapply && .venv/bin/python -m app.ops.tombstones purge && .venv/bin/python -m app.ops.verify_restore  # after a restore, before traffic
.venv/bin/python -m tests.e2e.restore_drill    # local drill (deployment/.env sourced; scratch DBs and bucket)
.venv/bin/python -m tests.e2e.demo             # browser demo at http://127.0.0.1:8770 (fake Google/GitHub; own DB and bucket)
.venv/bin/python -m app.ops.loadcheck --base <url> --site <slug> --users 50   # NFR p95 targets (see README)
.venv/bin/python -m app.ops.billing reconcile|process   # re-sync Stripe customers / process webhook receipts now
.venv/bin/python -m app.ops.billing clear-dispute <workspace-id> "<reason>"   # unblock generation after a lost dispute
python3 scripts/check_versions.py        # VERSION == backend/frontend/lockfile/OpenAPI versions
cd deployment && cp .env.example .env && mkdir -p secrets && touch secrets/github-app.pem \
  && docker compose --env-file .env -f compose.yaml --profile app up -d --build   # http://localhost:8080
```

Conventions that span files:

- **Dependencies.** Python locks are hash-pinned and resolved only from releases before `[tool.uv] exclude-newer` in `backend/pyproject.toml`; regenerate them with the `uv pip compile` commands in README, never by hand. CI fails if the locks differ from a fresh compile. npm uses exact versions with a `before` cutoff in `frontend/.npmrc`. Move either cutoff forward deliberately, not as a side effect.
- **Versions.** The application version lives in the root `VERSION`, and `backend/pyproject.toml`, `backend/app/__init__.py`, `frontend/package.json` and the regenerated OpenAPI file must match it. The spec has its own line in `build-package/VERSION`. Root `SPEC_VERSION` names the spec release the code conforms to. Bump it only when the code actually implements that release, never just because the spec moved.
- **Two copies of the deployment templates.** `deployment/` is the live copy; `build-package/deployment/` is the spec's reference copy. Change both together and record the change in the spec changelog.
- **Migrations** run only as the `migrate` job with the DDL role (`MIGRATION_DATABASE_URL`), never at API startup. New revisions use sequential IDs (`alembic revision --rev-id 0003 -m "..."`). Larger DDL lives in `NNNN_<name>.sql` and `NNNN_<name>_down.sql` beside the revision, executed through the raw DB-API cursor (see `0002_tenant_core.py`); SQLAlchemy's `exec_driver_sql` would parse `%` as placeholders.
- **Schema and models.** The migration SQL is authoritative for constraints, RLS, grants and `app_private` functions. `app/db/models.py` mirrors the columns, and `tests/integration/test_schema.py` fails on drift. Every new table needs FORCE RLS, explicit grants, a model and isolation tests in `tests/integration/test_tenancy.py`, using the `world` fixture of two tenants.
- **Auth in handlers.** Use the dependencies in `app/modules/identity/sessions.py`: `Db`, `OptionalPrincipal`, `CurrentPrincipal`, or `CsrfPrincipal` for every unsafe method, which checks Origin and X-CSRF-Token. They set `app.user_id` for the request transaction. Handlers commit explicitly. Browser-navigation endpoints (OAuth start and callback) redirect with `?error=<code>` instead of returning JSON. Sign-in tests fake providers through `httpx.MockTransport` with real RS256 tokens (`tests/integration/test_auth.py`), never by stubbing out verification.
- **Jobs** (`app/jobs`). Create work only through `store.create_task`, which writes the outbox row in the same transaction. Stage handlers in `stages.py` must not hold a transaction during GitHub, sandbox or storage I/O; they return an `apply` callback that `store.complete` runs after the fencing check. The reconciler (`control.py`, app_system) re-notifies runnable tasks; never requeue from the broker. New stages go into `store.STAGES` and `stages.HANDLERS`.
- **Generation** (`app/jobs/generation.py`, `verification.py`, `calls.py`).
  - Every provider call goes through `calls.run`, which follows a fixed sequence: earmark inside the build envelope, call with no transaction open, then settle actual usage, the ledger entry and the cache checkpoint in one fenced transaction.
  - Lock order is the build row, then the task row.
  - Holds: each call earmarks its worst case. A call that fits once the build's in-flight calls settle waits (`calls.EnvelopeBusy`, up to `HOLD_WAIT_SECONDS`) instead of ending the build. After a stop, `generation.bounded` starts no new unit but lets running ones finish and settle. A call cut off by cancellation settles `unknown` (`call_cancelled`). The hourly retention sweep settles `unknown`/`started` attempts older than 24 h at their maximum (`ledger.settle_stale_hold`, state `reconciled`).
  - Never classify a connection that may have sent bytes as transient; only a connect error or connect timeout is (the Anthropic SDK chains `httpx2` errors).
  - Unknown outcomes are never retried within a call (each try would add a worst-case hold). A writer call with no answer (transient after retries, or unknown) stops the stage as `provider_unavailable`, so the task retries from its checkpoints; only refused or invalid answers fall back to replay text. Checker calls without an answer leave claims `unchecked`.
  - Prompts are immutable per set in `app/engine/prompts/<X.Y.Z>/`; any text change is a new set and a new `GENERATION_PROMPT_VERSION`.
  - The checker never falls back to a heuristic for model output; any checker failure leaves claims `unchecked`. A truncated checker answer is not repaired (`calls.run(..., repair_truncated=False)`); `verification._run_checker` splits the batch, at most `MAX_CHECK_SPLITS` times.
  - Get gateways through `generation.gateway_for`, never `rt.gateways[...]`: own-key builds use the workspace's key, looked up on every call.
  - Every Anthropic client names its endpoint (`OFFICIAL_BASE_URL`, or `ANTHROPIC_BASE_URL` for the operator's key only), so the SDK never falls back to the environment or a credentials profile; own keys always go to the official API. The other providers' adapters use the runtime's shared httpx client with full URLs (`OPENAI_BASE_URL` only for the operator's OpenAI key); own-key gateways come from `runtime.own_key_gateways` (`rt.own_key_gateway(provider, key)`).
  - A new provider needs: the catalog `provider` Literal, a gateway, `operator_gateways`/`own_key_gateways`, `Settings.configured_providers`, `keys.Provider` plus a `verify_key`, and a migration widening `provider_keys.provider`.
  - Writer requests carry `audience` (`_writer_request`); the checker never does. Cache keys include the variant.
  - A request input a prompt set's template does not read (checked with `prompts.uses`) stays out of its cache key, so engine changes do not invalidate older sets' cached outputs (the guide page's `GUIDE_PLAN` and `DEFINITIONS`, read from 0.5.0).
  - Guide pages are validated and stored through `generate.normalize_citations` (`[[id:N]]` becomes `[[id:N-N]]`); any other malformed marker fails `validate_guide`, so it is repaired rather than stored. Provider calls are bounded at 300 s (15 s to connect, `_http.CALL_TIMEOUT_SECONDS`).
  - Batches go only through `batching.prefill`. Batched attempts are `batched`, and waiting raises `StageWait`, which does not use up an attempt. A gateway raises `batch_not_sent` only when the provider cannot have received the batch (its holds are released); any other submit failure abandons it with holds kept.
  - Regenerate `backend/evals/reports/baseline-replay.json` after changing parser, prompts, chunking or claim extraction (CI diffs it).
- **Engine** (`app/engine`). Repository content is parsed only inside the sandbox (`sandbox.run("app.engine.parse_worker", ...)`); add new sandboxed modules to `sandbox_exec.ALLOWED_MODULES` only. `app/engine/schemas/explanation.schema.json` must equal the spec contract (a test checks it). Object keys come only from `engine/keys.py`. SCIP indexers run only through `indexers.resolve` (scratch checkout of selected files plus, for Rust, the Cargo manifests beside it; the worker's own tsconfig/pyrightconfig/rust-analyzer settings; `sandbox.run(..., indexer=...)`); their packages are pinned in `backend/indexers/package-lock.json`, installed with `npm ci --ignore-scripts` under the same `before` cutoff as the frontend.
- **Privileges and operations.**
  - Every runtime privilege is granted explicitly in its migration; local bootstrap defaults are not relied on. `test_migrations_alone_grant_the_runtime_exactly_its_privileges` compares a database migrated without defaults.
  - Revoke what defaults would add on tables the runtime must not write (0018, 0020).
  - Operator actions go only through `app_private.ops_*` functions, which re-check the role and write both logs.
  - Support access is read-only: writer dependencies pass `write=True` to `resource_member`/`_member`.
- **Rate limits** use `ratelimit.hit(request.app.state.rate_sessions, ...)`, a separate pool so a request never waits for a second connection of its own. Client addresses come from `ratelimit.client_address`: trusted proxies only, IPv6 per /64.
- **Deletion and retention.** Content purges keep the ledger's rows (builds, tasks, attempts, snapshots) with names scrubbed. Re-check publication pointers after locking, in a separate statement (READ COMMITTED snapshots). Operator takedowns are never deleted.
- **Owner notices.** Record a new kind with `notices.record(db, ws, kind, dedupe_key, details)` inside the transaction that changed the state, add it to the `kind` CHECK (a migration), `messages.SUBJECTS` and `messages.body`. Details hold identifiers and reason codes only; names are read at send time. Subjects are fixed text; user-chosen names go only in the body, through `messages.clean`.
- **Billing.** Never grant from a redirect or event metadata. The workspace comes from `billing_customers` through `app_private.billing_workspace`, and state from the provider's subscription list (`sync.apply_subscriptions`). Never edit `subscriptions` by hand: fix the subscription in Stripe and reconcile. Refunds and disputes are audited and never touch `usage_entries`. Blank optional settings (`KEY=`) are unset (`Settings._blank_is_unset`).
- **Errors** go through `app/core/errors.py` (`ApiError` → envelope). Never return exception text, input values or connection details to clients.
- **Frontend environment.** Code may read only the `import.meta.env` keys allowlisted in `frontend/scripts/check-env-usage.mjs`.

Backend process entry points:

- `uvicorn app.main:app` (API)
- `python -m app.workers.main` (worker)
- `python -m app.workers.dispatcher` (outbox dispatcher)
- `python -m app.workers.scheduler` (scheduler)
- `alembic upgrade head` (migrations; run as the DDL role)

Frontend: `npm ci && npm run build`. Health check: `/api/v1/health/ready`.

## Architecture rules that span multiple files

- **Modular monolith.** The FastAPI control plane, outbox dispatcher, scheduler and generation workers all share PostgreSQL. Modules talk through typed service calls, not internal HTTP. Never run builds in FastAPI `BackgroundTasks`.
- **PostgreSQL is the source of truth for jobs.** RabbitMQ only carries notifications. Messages hold task, build and workspace IDs, a schema version and a trace ID, never tokens or code.
  - Build, task, budget reservation and outbox row are created in one transaction. The dispatcher publishes with `FOR UPDATE SKIP LOCKED` and publisher confirms.
  - Consumers deduplicate by task ID and lease generation (fencing). A worker that has lost its fencing generation must not commit artifacts or ledger entries.
  - A reconciler re-creates notifications for runnable tasks that have no live lease. Retries are persisted in SQL as `next_attempt_at`; there are no nack/requeue loops.
- **Tenancy is enforced in three layers.**
  - Every tenant row has `workspace_id NOT NULL`, with composite FKs that include it.
  - RLS uses `FORCE ROW LEVEL SECURITY` with `SET LOCAL app.workspace_id` per transaction. The value comes from the authorized session or the claimed task, never from request data or source content.
  - Every query still filters `workspace_id = :w` explicitly.
  - Project-scoped content must go through `resource_member("project"|"build"|"export", ...)`, which applies per-project access (`project_member`). New project-scoped checks outside it use `app_private.project_role`. A restrictive row policy on `projects` is the backstop for requests that act for a user. RLS is the backstop, and the user-scoped SELECT policies widen what a workspace context shows (they once leaked memberships across workspaces). Workspace routes use `ReadMember`/`WriteMember` from `app/modules/workspaces/access.py`: non-members get 404, insufficient roles 403.
  - Set context with `set_config('app.workspace_id', $1, true)` (bound parameters), plus `app.user_id` for user-scoped lists. Use `app/core/tenancy.py` (`set_context`, `resource_workspace`) at the start of every transaction; settings are transaction-local.
  - Roles: `app_runtime` (API and workers; not owner, no BYPASSRLS), `app_system` (dispatcher/scheduler; outbox and task-scheduling columns only), `app_resolver` (NOLOGIN owner of SECURITY DEFINER lookups such as `app_private.resource_workspace` for flat routes like `/projects/{pid}`). No role bypasses RLS. Migrations use a separate DDL role. Never use ORM `create_all`. See `05_DATA_MODEL.md` "Tenant context resolution".
- **No open DB transaction across external I/O.** That covers GitHub, model HTTP and S3 calls. Use one `AsyncSession` per request or task.
- **The engine is pure and versioned.** It has no HTTP-session concepts. The stages run in order: scan → parse → rank → explain → overview → plan → guide → citation verify → semantic check → render → manifest.
  - Repository content is treated as data. Prompt-injection resistance is a tested requirement.
  - Parsers run in a sandbox with no network and no credentials. This sandbox is an M1 gate.
  - All external integrations sit behind `adapters/` and have replay fakes. Hosted mode doesn't accept user-supplied model base URLs.
- **Artifacts.** Snapshots are immutable and pinned to an exact commit SHA. Objects live under `workspaces/{wid}/projects/{pid}/builds/{bid}/`. The manifest is uploaded last, and a publication is a DB pointer to a ready build. Don't serve tenant-generated HTML from the auth origin.
- **Money and budgets.**
  - Amounts are integer USD micro-units (`*_micro_usd`, bigint). Never use floats.
  - Reserve the build cap once, earmark each provider call inside that reservation, then settle idempotently.
  - `usage_entries` and `audit_events` are append-only. Corrections are reversal entries.
- **API conventions** (`06_API_SPEC.md`).
  - Error envelope: `{error:{code,message,request_id,retryable,details}}`. Return 404 for objects the caller isn't authorized to see.
  - Cursor pagination returns `{items,next_cursor}`.
  - Creates require an `Idempotency-Key` header, and config/publication writes require `If-Match` ETags.
  - Unsafe methods require `X-CSRF-Token` plus an Origin check.
  - The TypeScript client is generated from FastAPI's OpenAPI 3.1 output, and CI checks it for drift.
- **Auth.** Opaque session cookie `__Host-codehover_session`; only its SHA-256 digest is stored. Identity comes from the provider subject; accounts are never merged by email. GitHub sign-in (identity OAuth app) is separate from the read-only GitHub App (repository access). Installation ownership is verified with a short-lived user token from the GitHub App itself (`GET /user/installations`), because the identity app's token cannot prove it.
- **Model output.** `contracts/explanation.schema.json` v2 keeps every object closed and has no open maps, as strict structured-output modes require. Length and count limits are enforced by the local validator.
