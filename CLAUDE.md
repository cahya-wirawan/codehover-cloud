# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

The repository holds only a planning package in `build-package/`. There is no application source and there are no lockfiles yet. codehover-cloud is a planned multi-tenant SaaS that turns a GitHub repository into a browsable, cited explanation website, with hover cards, reference graphs and architecture guides. It is a hosted version of the `cahya-wirawan/codehover` CLI. Its engine is written independently: reusing any reference code needs an explicit decision and a license review.

The numbered documents in `build-package/` are normative requirements. `CODEHOVER_WEB_MASTER_SPEC.md` is generated from them, so edit the numbered files, then regenerate it (see Commands). Record every package change under `## [Unreleased]` in `build-package/CHANGELOG.md`. `build-package/VERSION` is the only hand-edited version. Choose the bump from the package rules in `23_VERSIONING_AND_RELEASES.md`, which also defines versioning for the product itself: API path major, contract `$id` URNs, engine and prompt-set SemVer, presets and catalogs. The package is in initial development (0.y.z) until an M1 implementation conforms. Until then, an incompatible contract change (a removed or renamed field, endpoint or setting, a new required setting, or a stricter schema) bumps MINOR, and additive or editorial changes bump PATCH. Never bump to 1.0.0 without the user's say-so. Reading order: `01_PRD` → `04_SYSTEM_ARCHITECTURE` → `05_DATA_MODEL` → `06_API_SPEC` → `07_AI_OR_AUTOMATION_PIPELINE` → `15_PRODUCT_BACKLOG`. For UX, read 02, 03 and 17. For ops, read 22 and `deployment/`. The machine-readable contracts are in `build-package/contracts/`: `core_schema.sql` is a DDL subset showing tenancy, RLS and outbox patterns, not a complete migration; the folder also holds the explanation and task-envelope JSON Schemas. Prompts live in `19_PROMPT_LIBRARY.md`.

Scope of the first implementation: **M0–M1 only** (`14_IMPLEMENTATION_ROADMAP.md`). That means the foundation plus a private vertical slice that uses the deterministic replay model adapter:

- OAuth sign-in
- workspace membership with RLS
- GitHub App acquisition of immutable snapshots
- safe scan
- outbox, RabbitMQ and worker
- replay build and private viewer

Don't add paid model calls or billing until the cross-tenant tests and the worker crash/retry tests pass.

## Stack

- **Backend:** Python 3.13, FastAPI, Pydantic 2, SQLAlchemy 2.x async, psycopg 3, Alembic, aio-pika (explicit consumers, no Celery), httpx, Authlib.
- **Data:** PostgreSQL 18 with native `uuidv7()` PKs (no UUID extension). RabbitMQ 4. S3-compatible private storage (MinIO locally). No Redis.
- **Frontend:** React, TypeScript, Vite, Tailwind, shadcn/ui, TanStack Query, React Router, React Hook Form with Zod, and a read-only CodeMirror 6 viewer.
- **Tests:** pytest and pytest-asyncio against real PG18 and RabbitMQ containers; Vitest with RTL; Playwright with axe.

Planned layout (`16_REPO_STRUCTURE.md`): `backend/app/{core,modules/<domain>,workers,engine/<stage>,adapters/<integration>}`, `backend/alembic/`, `backend/tests/{unit,integration,security,fixtures}`, `frontend/src/{app,features,components,lib}`, `frontend/e2e/`, `deployment/`, `contracts/`.

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

These are the commands the container contract expects (`build-package/22_DEPLOYMENT_AND_OPERATIONS.md`). The application profile can't run until `backend/` and `frontend/` exist with `backend/requirements.lock` and `frontend/package-lock.json`.

```bash
# Infrastructure only (works today, from a copy of deployment/ at repo root)
cp .env.example .env            # set unique secrets; never commit .env
docker compose --env-file .env -f compose.yaml config
docker compose --env-file .env -f compose.yaml up -d db rabbitmq objectstore
# Create private bucket "codehover" via MinIO console at localhost:9001

# Full app (after implementation)
docker compose --env-file .env -f compose.yaml --profile app build
docker compose --env-file .env -f compose.yaml --profile app up -d   # http://localhost:8080
```

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
  - Set context with `set_config('app.workspace_id', $1, true)` (bound parameters), plus `app.user_id` for user-scoped lists.
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
