# Changelog

All notable changes to this build package. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/); versions follow the "Build package" rules in `23_VERSIONING_AND_RELEASES.md`. New entries go under Unreleased, grouped as Added, Changed, Deprecated, Removed, Fixed or Security; `python3 tools/assemble.py bump <major|minor|patch>` turns them into a release.

## [Unreleased]

## [0.3.5] - 2026-11-17

### Changed

- Decisions made while implementing sign-in (T03) are now in the spec:
  - 09: the CSRF derivation, the exact-Origin rule, the session touch interval, reauthentication by signing in again, and the development cookie name;
  - 06: the `return_to` parameter, callback error redirects, authentication error codes, and `no-store` on API responses;
  - 05: sealed PKCE verifier and nonce storage.

## [0.3.4] - 2026-11-17

### Added

- 05 adds the `idempotency_keys` table: 06 required `Idempotency-Key` storage, but the data model had no table for it.
- 05 adds the pre-authentication lookups `identity_user`, `create_user_with_identity`, `session_by_token` and `oauth_transaction_create/consume`, with their RLS rules. These were found while implementing migration 0002 (T02).

## [0.3.3] - 2026-11-17

### Added

- Implementations record the package version they conform to in a root `SPEC_VERSION` file. CI validates it against the package changelog, and images carry it as the label `codehover.spec.version`. This is defined in 23 and reflected in 16 and 18; the `Dockerfile.*` templates gain the `SPEC_VERSION` build argument and label.

## [0.3.2] - 2026-11-17

### Added

- Host-port overrides in the Compose templates (`DB_HOST_PORT`, `AMQP_HOST_PORT`, `RABBITMQ_MANAGEMENT_HOST_PORT`, `S3_HOST_PORT`, `WEB_HOST_PORT`), with unchanged defaults, so the stack starts next to a local PostgreSQL or RabbitMQ.

### Changed

- `VALIDATION_REPORT.md` records the first real run of the container templates: image builds, Compose start-up, the gateway, SeaweedFS and RabbitMQ, using the M0 implementation.

## [0.3.1] - 2026-11-17

### Changed

- The local object store is decided: SeaweedFS 4.52 (`weed mini`), pinned by digest. This replaces the archived MinIO image in the `deployment/` templates, 21 and 22.
- Deployment templates gain:
  - a `bootstrap` Compose service, with the dead-letter exchange `codehover.dlx` and the `codehover.work.*` queue policy;
  - `APP_ENV` and `RABBITMQ_MANAGEMENT_URL` settings;
  - OCI version labels on images;
  - pip installs from the hash-pinned lock with `--no-deps`.

  The templates now match the implemented repository copy from M0.

## [0.3.0] - 2026-11-17

### Added

- `23_VERSIONING_AND_RELEASES.md`: one SemVer-based registry, with a 0.y.z initial-development rule (incompatible changes bump MINOR until 1.0.0). It covers the package, application releases, the HTTP API, JSON contracts, the queue envelope, stored artifacts, the engine, parser and prompt sets, model presets, price catalogs and database migrations. It also sets compatibility, deprecation and cache rules and the release process.
- `VERSION` as the package's single hand-edited version; `tools/assemble.py bump` and stricter `--check` (SemVer format, changelog order, version agreement across generated files, contract `$id` consistency).
- `$id` URNs with SemVer on `contracts/explanation.schema.json` (2.0.0) and `contracts/task-envelope.schema.json` (1.0.0).

### Changed

- 05, 06, 07, 10, 16, 18 and 19 now point to the version registry. The prompt directory is `prompts/<X.Y.Z>/`, the KB export identifies itself by schema `$id`, and `/health/live` reports the application version.

## [0.2.0] - 2026-11-17

Review revision, first circulated as "1.1" and briefly as "2.0.0". It changes contracts incompatibly, which under the 0.y.z rule is a MINOR bump:
- explanation schema v1 → v2;
- `DELETE /projects/{pid}/publication` takes `If-Match` instead of a body;
- new required settings `APP_ENCRYPTION_KEY` and `APP_SYSTEM_DB_PASSWORD`;
- the database role model changed.

The 0.1.0 package was reviewed for correctness and internal consistency. Its reference SQL was then executed on PostgreSQL 18.0. Fixes are grouped by severity. Numbers refer to the documents changed.

### Design gaps that would have blocked M1

- **FORCE RLS made the control loops and flat routes impossible (04, 05, 22, `contracts/`, `deployment/`).** With every tenant table under FORCE RLS, three things failed:
  - the dispatcher, scheduler and reconciler could not see any outbox or task rows;
  - `/projects/{pid}`-style routes could not find their workspace before setting the context;
  - `GET /workspaces` could not list a user's own workspaces, and new workspaces could not be inserted at all.

  Added:
  - an `app_system` role limited to the outbox and task-scheduling columns;
  - a NOLOGIN `app_resolver` role owning SECURITY DEFINER lookups (`resource_workspace`, `resolve_publication`, `create_workspace`);
  - user-scoped SELECT policies via `app.user_id`.

  No role uses BYPASSRLS. Bootstrap and compose now create and use these roles.
- **Installation verification could not work as specified (06, 09, 22, `.env.example`, `compose.yaml`).** `GET /user/installations` requires a user access token from the GitHub App itself. The identity OAuth app's token cannot prove a user's access to an installation. The setup flow now gets a short-lived GitHub App user token, and the App's client ID and secret are added to the settings.
- **The explanation schema was incompatible with strict structured outputs (`contracts/explanation.schema.json`, 05, 06, 19).** `params` was an open map (`additionalProperties` as a schema), which strict modes reject. It is now an array of `{name, description}` (schema v2). Length and count keywords are enforced by the local validator, not sent to providers that reject them.
- **The parser sandbox was an M1 gate in 22 but missing from the roadmap and backlog (14, 15).** It is now part of M1 deliverables and exit criteria, and of T08.

### Correctness and security fixes

- **nginx (`deployment/nginx.conf`, 08, 22):**
  - Security headers were silently dropped on `/assets/`, because a location with its own `add_header` stops inheriting the server-level ones.
  - Added the baseline CSP that 08 requires but no template set.
  - The SPA shell is now `no-cache`.
  - The one-hour read timeout and disabled buffering now apply only to the SSE route; other API routes get 60s timeouts.
  - Client-supplied `X-Forwarded-*` headers are overwritten instead of passed through.
  - The upstream is re-resolved through Docker DNS.
- **Unchecked claims contradicted build immutability (07, 20 A13).** A13 said publish stays blocked "until execution completes", but terminal builds never re-run. Now a retry build re-runs the checks, reusing cached generation.
- **Concurrency was contradictory (07).** "4 per build and 2 per workspace" could never reach 4. Clarified as 4 concurrent provider calls per build, plus the active-build limits from 01.
- **Column convention was unspecified (02, 05, 06, 07).** Columns are now 0-based and end-exclusive, counted in Unicode scalar values. Tree-sitter UTF-8 byte offsets are converted once at the parse stage.
- **Tenant setting (05).** Use `set_config('app.workspace_id', $1, true)` with bound parameters. `SET LOCAL` cannot take parameters and invites string formatting.
- **API (06).** `DELETE /projects/{pid}/publication` now uses `If-Match` instead of a request body.
- **RabbitMQ 4 (04, 22).** Quorum queues drop messages after 20 deliveries unless a dead-letter exchange is configured. A DLX policy is now required.
- **Missing data-encryption key (22, `.env.example`, `compose.yaml`).** Added `APP_ENCRYPTION_KEY` for encrypted PKCE verifiers and webhook payloads, kept separate from `SESSION_SECRET`.
- **Build context leakage (`Dockerfile.backend`, new `*.dockerignore`, 16).** The repo-root build context would have sent `deployment/.env` and `secrets/` to the builder. Allowlist ignore files are added. Backend code is no longer owned by the runtime user.
- **Accessibility (17).** `border` reaches only about 1.3:1 in light mode. Added an `input-border` token (at least 3:1 on every surface) for control boundaries.
- **Local object store (21, 22, `.env.example`).** MinIO community images stopped in October 2025 and the project was archived. This is recorded as a risk and an M0 decision.

### Consistency

- Build-scoped frontend routes use one prefix, `/p/:pid/builds/:bid/...` (02).
- Claim extraction is now defined deterministically (07). Multi-chunk file summaries are defined (07).
- `core_schema.sql` gains:
  - task state and publication slug CHECKs;
  - a `reversal` ledger kind (05 and 11 already referenced reversal entries);
  - memberships;
  - REVOKE of runtime UPDATE/DELETE on the ledger.
- Added `contracts/core_schema_checks.sql`: executable tenancy, role and integrity assertions, mutation-tested.
- Package manifest: the 0.1.0 manifest checksums were already stale for 7 files and the master spec, despite that release's validation claim. `tools/assemble.py` now regenerates the master spec and manifest, and checks them.

## [0.1.0] - 2026-11-17

Initial planning package (originally labelled "1.0"; renumbered because an unimplemented specification is initial development under SemVer): documents 00–22, reference SQL subset, JSON schemas and container templates.
