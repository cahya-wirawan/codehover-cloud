# Docker deployment and operations

## Deliverable boundary

`deployment/compose.yaml`, Dockerfiles and nginx/broker config are concrete reference configurations for the specified future application. They are syntax-checked templates, not a tested running SaaS. Application source, dependency lockfiles, OAuth apps and credentials must be implemented/configured first. Infrastructure services can start without application source. No repository changes or deployment were performed.

## Local workflow

1. Copy `deployment/` into the implemented repo root as `deployment/`; keep `backend/` and `frontend/` alongside it.
2. Copy `.env.example` to `.env` inside deployment, set unique local secrets and choose reviewed image tags. Never commit `.env`.
3. Run `docker compose --env-file .env -f compose.yaml config` from deployment.
4. Start infrastructure: `docker compose --env-file .env -f compose.yaml up -d db rabbitmq objectstore`.
5. Create the private `codehover` bucket via local S3 console (localhost:9001), with anonymous access disabled. Implementation M0 adds an idempotent bootstrap command for buckets/runtime DB role/broker policies.
6. After app and exact locks exist: `docker compose --env-file .env -f compose.yaml --profile app build`, then `docker compose --env-file .env -f compose.yaml --profile app up -d`.
7. Open `http://localhost:8080`. Same-origin API `/api/v1`; local OAuth redirect URLs are `http://localhost:8080/api/v1/auth/google/callback` and the GitHub equivalent. Enable localhost development cookie mode explicitly; production requires TLS/secure cookie.

Production OAuth URLs use exact HTTPS public origin. Configure Google consent/allowed redirects and GitHub OAuth identity app separately from GitHub repository App. Repository App webhook is `/api/v1/webhooks/github`, setup callback `/api/v1/integrations/github/setup`. Install App private key through mounted secret or secret manager; `.env` file path merely configures that mount in local template. GitHub App contents/metadata read permissions only. Identity login does not imply repository read access.

## Container interface and configuration

Backend Dockerfile expects `backend/requirements.lock` as exact pip requirements, and package code in `backend/app`, Alembic config in `backend/alembic.ini` + `alembic/`. Commands: API `uvicorn app.main:app`; workers `python -m app.workers.main`; dispatcher `python -m app.workers.dispatcher`; scheduler `python -m app.workers.scheduler`; migration `alembic upgrade head`. Settings must support `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `AMQP_URL`, `S3_*`, `PUBLIC_ORIGIN`, `SESSION_COOKIE_SECURE`, OAuth credentials, GitHub App key file/webhook secret, and approved model settings. API/worker use `app_runtime`; migration uses DDL role. Runtime roles are created by local init SQL for clean volumes. Initialization SQL does not rerun on existing volumes; rotate/migrate credentials deliberately.

Frontend Dockerfile expects `frontend/package.json` and committed `package-lock.json`, runs `npm ci` then `npm run build`, serving `dist/` via unprivileged nginx on 8080. VITE config contains public API path only; provider secrets stay on server. Gateway disables SSE buffering, applies request-size/time limits and forwards trusted headers only. Production ingress terminates TLS; set secure cookie/HSTS and restrict proxy header trust to ingress IPs, never accept arbitrary external forwarded hosts.

PostgreSQL 18 volume mounts `/var/lib/postgresql` with explicit `PGDATA=/var/lib/postgresql/18/docker`; do not reuse a PostgreSQL 17 directory without an upgrade procedure. Native `uuidv7()` eliminates UUID extensions for primary keys. Separate migration and runtime roles; production bootstrap secrets managed outside checked-in SQL. SQLAlchemy maps UUID to Python UUID and obtains database-generated IDs using flush/RETURNING.

RabbitMQ local template is a single node for development; durable queues/messages + confirms/manual ack protect against ordinary process failures but not single-node disk loss. Configure named vhost, distinct users for publisher/consumer and least-permission resource regex in production. App declares quorum queues with dead-letter exchange where supported; production HA needs three nodes across failure domains. SQL reconciler can reconstruct job notifications; RabbitMQ is not the sole job record. Broker management binds localhost only. Queue maximum length and message TTL are set deliberately and alerts catch overflow; never silently discard work without SQL recovery.

Object store uses private bucket and per-service limited credentials; root console credential in Compose is local-only. Image tags in reference config are examples: verify availability/security and pin tested digests in the real deployment. Database/broker/object storage ports bind localhost for local debugging; production publishes none of them. Templates are not a network isolation policy for hostile repositories.

## Sandbox implementation gate

Read-only rootfs/cap_drop/non-root container options do not alone isolate parser subprocess network access from the worker's provider credentials. M1 must implement an explicit sandbox runner (for example a separate restricted parse service or OS-enforced user/network namespace with seccomp, rlimits, read-only input mount and empty environment). It receives only source bytes/grammar config, no tokens; returns validated JSON through bounded pipe/object reference. No shared host Docker socket, privileged flag or repository dependency installation. A network-isolation test is a required gate. SCIP remains P1 until safe indexer operation can be demonstrated without tenant install/hooks; incomplete indexing falls back with clear labels.

## Production runbook

Deploy sequence: build lockfile-based images → scan/SBOM/sign → backup/schema compatibility check → run one migration job → API rollout → dispatcher/scheduler → workers → synthetic private replay build → publish/read check → monitor. Rollback previous application image only if schema remains compatible. Avoid force-downgrading migrations on customer data. Pause new builds with operator kill switch while existing reads remain available during provider/cost incidents.

Recovery: managed PG continuous WAL/PITR with ≤15min target RPO; daily encrypted object inventory/versioning or equivalent recovery; daily logical dump as supplementary backup. Keep at most 35d source backups by default. Reconstruct unsent tasks from SQL, reconcile provider attempts and reservations before resuming. Restore into isolated environment; apply deletion tombstones; validate ownership/manifest checksums; measure RTO ≤4h; only then route traffic. Test quarterly and before paid launch. Object and database backups must be mutually recoverable; do not restore publication pointers whose artifacts are missing.

Troubleshooting: queued stuck → outbox lag/runnable tasks/broker routes; running stuck → lease heartbeat/fencing/provider timeout; ready cannot publish → quality report/current connection/ETag; missing site → publication suspension/deletion/object manifest; balance mismatch → ledger reconciliation and unresolved attempts. Never fix by bypassing tenancy or resetting balances without reversal entries and audit.

CI pipeline: backend format/lint/types and unit → PG18/Rabbit integration + Alembic/RLS → frontend lint/type/Vitest/build → Playwright/axe/replay eval → dependency/image scan → SBOM → artifact schema/OpenAPI drift → image registry → staging smoke → gated production release. Docker engine was unavailable during blueprint creation, so only YAML/JSON/Python/package consistency checks were performed here.
