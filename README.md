# codehover-cloud

codehover-cloud turns a GitHub repository into a browsable, cited explanation website. The specification lives in [`build-package/`](build-package/README.md); this repository implements it milestone by milestone (`build-package/14_IMPLEMENTATION_ROADMAP.md`).

**Status: M1 complete** (private vertical slice, deterministic replay generation). It includes:
- Google and GitHub sign-in with sessions and CSRF protection;
- workspaces with roles and invitations;
- GitHub App connections that grant only the repositories the connecting user can read;
- snapshot acquisition with bounded extraction and a safe scan;
- the outbox, RabbitMQ and worker job system, with fenced leases and a reconciler;
- a seccomp-sandboxed tree-sitter parser;
- replay explanations validated against the explanation contract;
- a private code viewer.

Live sign-in and builds need Google/GitHub OAuth credentials and a GitHub App (see `build-package/22_DEPLOYMENT_AND_OPERATIONS.md`); everything else, including the end-to-end build, is covered by tests with faked providers. M2 adds model-backed generation, estimates and budgets.

| Path | Contents |
|---|---|
| `backend/` | API (`app/main.py`), workers (`app/workers/`), infrastructure bootstrap (`app/ops/`), migrations (`alembic/`), tests |
| `frontend/` | React + TypeScript + Vite + Tailwind app |
| `contracts/openapi.json` | Generated API contract; CI fails if it drifts from the code |
| `deployment/` | Dockerfiles, Compose, nginx, broker config and local DB bootstrap |
| `VERSION` | Application version (SemVer, `build-package/23_VERSIONING_AND_RELEASES.md`) |

## Requirements

- Python 3.13 via [uv](https://docs.astral.sh/uv/)
- Node.js 24 and npm
- PostgreSQL 18 (local install or container)
- Docker, for the full stack

## Backend

```bash
cd backend
uv venv --python 3.13 .venv && uv pip sync --require-hashes requirements-dev.lock
.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/mypy
.venv/bin/pytest -m "not integration"                 # unit tests, no services needed
.venv/bin/pytest tests/unit/test_settings.py -k production   # a single test
```

Integration tests need a PostgreSQL 18 database bootstrapped with `deployment/init-db.sh`:

```bash
POSTGRES_USER=codehover_ddl POSTGRES_DB=codehover APP_DB_PASSWORD=runtimepw APP_SYSTEM_DB_PASSWORD=systempw \
  PGHOST=localhost sh ../deployment/init-db.sh
TEST_MIGRATION_DATABASE_URL=postgresql+psycopg://codehover_ddl:<pw>@localhost:5432/codehover \
TEST_DATABASE_URL=postgresql+psycopg://app_runtime:runtimepw@localhost:5432/codehover \
TEST_SYSTEM_DATABASE_URL=postgresql+psycopg://app_system:systempw@localhost:5432/codehover \
  .venv/bin/pytest -m integration
```

The test database must not be shared with a running worker, dispatcher or scheduler: they would claim the tests' outbox rows and tasks. The suite refuses to start when such processes are connected (`codehover-worker`, `codehover-dispatcher`, `codehover-scheduler` in `pg_stat_activity`). Against the Compose database, stop them first with `docker compose --env-file .env -f compose.yaml --profile app stop worker dispatcher scheduler`, or use a separate database.

To run the API locally:
1. Apply migrations with the DDL role: `MIGRATION_DATABASE_URL=... .venv/bin/alembic upgrade head`.
2. Start the server with the runtime role: `DATABASE_URL=... SESSION_SECRET=... APP_ENCRYPTION_KEY=... .venv/bin/uvicorn app.main:app --reload`.

After changing API routes or models, regenerate the contract with `.venv/bin/python -m app.openapi_export`.

**Generation.** Without configuration only the free `replay-v2` preset is offered. It runs the whole pipeline deterministically.

To offer a paid preset:
1. Copy `deployment/model-catalog.example.json`.
2. Confirm every price, region and retention entry.
3. Give the catalog its own revision.
4. Set `MODEL_CATALOG_FILE` and `ANTHROPIC_API_KEY` for the API and the worker.
5. Set `GENERATION_ALLOWANCE_MICRO_USD` (per workspace and month).

**Evaluation.** `.venv/bin/python -m evals.harness --out evals/reports/baseline-replay.json` runs the labeled corpus in `evals/`. It is free and deterministic, and CI diffs the report. See `evals/README.md` for the maintainer labeling step and the billed `--provider anthropic --allow-spend` run.

**Dependency locks.** `requirements.lock` (runtime, used by the image) and `requirements-dev.lock` are hash-pinned. They are resolved from `pyproject.toml`, accepting only releases published before `[tool.uv] exclude-newer`. To refresh them:
1. Move that date forward deliberately.
2. Run `uv pip compile pyproject.toml --universal --python-version 3.13 --generate-hashes --no-header -o requirements.lock`, and the same command with `--extra dev -o requirements-dev.lock`.

CI recompiles both locks and fails on any difference.

## Frontend

```bash
cd frontend
npm ci
npm run dev          # http://localhost:5173, proxies /api to 127.0.0.1:8000
npm run lint && npm run typecheck && npm test && npm run build
npx vitest run src/app/routes.test.tsx   # a single test file
npm run check:env    # allowlisted import.meta.env keys only; no secrets in the bundle
```

The end-to-end journey (`frontend/e2e/`, Playwright with axe) builds the frontend and starts a test-only server, `backend/tests/e2e/server.py`. That server runs the real API on the integration-test database with fake Google and GitHub providers and an inline worker. It needs the backend venv, a migrated database with the same `TEST_MIGRATION_DATABASE_URL` and `TEST_DATABASE_URL` as the integration tests, and an installed Google Chrome (`PW_CHANNEL=""` uses Playwright's bundled Chromium instead). Stop the stack's workers first, as for the integration tests.

```bash
npm run test:e2e
```

`frontend/.npmrc` pins exact versions and applies a `before` cutoff, the same cooling-off period as the Python locks.

## Full stack (Docker Compose)

```bash
cd deployment
cp .env.example .env                       # replace every placeholder secret
mkdir -p secrets && touch secrets/github-app.pem   # placeholder until a GitHub App exists
docker compose --env-file .env -f compose.yaml --profile app up -d --build
curl http://localhost:8080/api/v1/health/ready
```

Start-up order:
1. Infrastructure: PostgreSQL 18, RabbitMQ and SeaweedFS (local S3).
2. `migrate`: Alembic, DDL role.
3. `bootstrap`: the private bucket, plus the broker dead-letter exchange and work-queue policy.
4. The API, workers and the nginx gateway on port 8080.

`init-db.sh` creates the database roles only on a fresh volume. If a host port is already taken (for example by a local PostgreSQL on 5432), set `DB_HOST_PORT`, `AMQP_HOST_PORT`, `RABBITMQ_MANAGEMENT_HOST_PORT`, `S3_HOST_PORT` or `WEB_HOST_PORT` in `.env`.

## Versioning

There are two independent version lines (`build-package/23_VERSIONING_AND_RELEASES.md`):

- `VERSION` is the application version (0.x until the paid launch).
- `build-package/VERSION` is the specification version.

`SPEC_VERSION` links them by naming the spec release this code conforms to. When the spec moves ahead, update `SPEC_VERSION` once the code catches up. `python3 scripts/check_versions.py` verifies that the backend, frontend, lockfile and OpenAPI contract agree with `VERSION`, and that `SPEC_VERSION` is a real spec release no newer than the spec. Images carry both versions as labels.
