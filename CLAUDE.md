# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

M0 to M5 are implemented.

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
  - The plan lifecycle: past due or disputed blocks generation (402); long past due or ended suspends hosting with reason `billing`; there is a one-time trial (`app_private.start_trial`); plan limits apply, with `BILLING_ENFORCED` for projects and seats.
  - The Plan and usage page, plans on the start page, draft privacy/terms/support pages (`GET /meta`), the billing runbook and the accounting review (spec 22).
  - The payment failure matrix: `tests/integration/test_billing.py`, with `FakeStripe` over `httpx.MockTransport`.

The eval labels are drafts until two maintainers label them. Live use needs real Google/GitHub OAuth and GitHub App credentials, plus an operator model catalog and provider key for paid presets. The specification lives in `build-package/`. codehover-cloud is a planned multi-tenant SaaS that turns a GitHub repository into a browsable, cited explanation website, with hover cards, reference graphs and architecture guides. It is a hosted version of the `cahya-wirawan/codehover` CLI. Its engine is written independently: reusing any reference code needs an explicit decision and a license review.

The numbered documents in `build-package/` are normative requirements. `CODEHOVER_WEB_MASTER_SPEC.md` is generated from them, so edit the numbered files, then regenerate it (see Commands). Record every package change under `## [Unreleased]` in `build-package/CHANGELOG.md`. `build-package/VERSION` is the only hand-edited version. Choose the bump from the package rules in `23_VERSIONING_AND_RELEASES.md`, which also defines versioning for the product itself: API path major, contract `$id` URNs, engine and prompt-set SemVer, presets and catalogs. The package is in initial development (0.y.z) until an M1 implementation conforms. Until then, an incompatible contract change (a removed or renamed field, endpoint or setting, a new required setting, or a stricter schema) bumps MINOR, and additive or editorial changes bump PATCH. Never bump to 1.0.0 without the user's say-so. Reading order: `01_PRD` → `04_SYSTEM_ARCHITECTURE` → `05_DATA_MODEL` → `06_API_SPEC` → `07_AI_OR_AUTOMATION_PIPELINE` → `15_PRODUCT_BACKLOG`. For UX, read 02, 03 and 17. For ops, read 22 and `deployment/`. The machine-readable contracts are in `build-package/contracts/`: `core_schema.sql` is a DDL subset showing tenancy, RLS and outbox patterns, not a complete migration; the folder also holds the JSON Schemas for explanations, overview, guide plan and page, claim checks and repairs, and task envelopes. Prompts live in `19_PROMPT_LIBRARY.md`.

Next scope: **M6** (`14_IMPLEMENTATION_ROADMAP.md`), P1 features, each behind demand evidence: webhook rebuilds, language and level variants, sandboxed SCIP, batch/BYOK and ZIP upload. Known gaps:
- event-stream concurrency limits are still per process;
- RPO ≤ 15 min needs a PITR drill on the managed staging database (the local drill covers the restore procedure and RTO only);
- the load numbers come from a single local process, not staging;
- the payment failure matrix has run only against the replayed provider, not Stripe test mode in staging;
- the legal texts are drafts awaiting jurisdiction review;
- owner notices are in-app only (there is no email delivery).

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
cd frontend && npm ci && npm run lint && npm run typecheck && npm test && npm run build && npm run check:env
cd frontend && npm run test:e2e           # Playwright + axe journey; same TEST_* URLs as integration, installed Chrome
.venv/bin/python -m app.ops.operators grant <user-id> "<note>"   # operations role (DDL role: MIGRATION_DATABASE_URL)
.venv/bin/python -m app.ops.tombstones reapply && .venv/bin/python -m app.ops.tombstones purge && .venv/bin/python -m app.ops.verify_restore  # after a restore, before traffic
.venv/bin/python -m tests.e2e.restore_drill    # local drill (deployment/.env sourced; scratch DBs and bucket)
.venv/bin/python -m tests.e2e.demo             # browser demo at http://127.0.0.1:8770 (fake Google/GitHub; own DB and bucket)
.venv/bin/python -m app.ops.loadcheck --base <url> --site <slug> --users 50   # NFR p95 targets (see README)
.venv/bin/python -m app.ops.billing reconcile|process   # re-sync Stripe customers / process webhook receipts now
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
  - Prompts are immutable per set in `app/engine/prompts/<X.Y.Z>/`; any text change is a new set and a new `GENERATION_PROMPT_VERSION`.
  - The checker never falls back to a heuristic for model output; any checker failure leaves claims `unchecked`.
  - Regenerate `backend/evals/reports/baseline-replay.json` after changing parser, prompts, chunking or claim extraction (CI diffs it).
- **Engine** (`app/engine`). Repository content is parsed only inside the sandbox (`sandbox.run("app.engine.parse_worker", ...)`); add new sandboxed modules to `sandbox_exec.ALLOWED_MODULES` only. `app/engine/schemas/explanation.schema.json` must equal the spec contract (a test checks it). Object keys come only from `engine/keys.py`.
- **Privileges and operations.**
  - Every runtime privilege is granted explicitly in its migration; local bootstrap defaults are not relied on. `test_migrations_alone_grant_the_runtime_exactly_its_privileges` compares a database migrated without defaults.
  - Revoke what defaults would add on tables the runtime must not write (0018, 0020).
  - Operator actions go only through `app_private.ops_*` functions, which re-check the role and write both logs.
  - Support access is read-only: writer dependencies pass `write=True` to `resource_member`/`_member`.
- **Rate limits** use `ratelimit.hit(request.app.state.rate_sessions, ...)`, a separate pool so a request never waits for a second connection of its own. Client addresses come from `ratelimit.client_address`: trusted proxies only, IPv6 per /64.
- **Deletion and retention.** Content purges keep the ledger's rows (builds, tasks, attempts, snapshots) with names scrubbed. Re-check publication pointers after locking, in a separate statement (READ COMMITTED snapshots). Operator takedowns are never deleted.
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
  - Every query still filters `workspace_id = :w` explicitly. RLS is the backstop, and the user-scoped SELECT policies widen what a workspace context shows (they once leaked memberships across workspaces). Workspace routes use `ReadMember`/`WriteMember` from `app/modules/workspaces/access.py`: non-members get 404, insufficient roles 403.
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
