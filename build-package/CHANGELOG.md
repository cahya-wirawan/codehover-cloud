# Changelog

All notable changes to this build package. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/); versions follow the "Build package" rules in `23_VERSIONING_AND_RELEASES.md`. New entries go under Unreleased, grouped as Added, Changed, Deprecated, Removed, Fixed or Security; `python3 tools/assemble.py bump <major|minor|patch>` turns them into a release.

## [Unreleased]

## [0.5.8] - 2026-11-18

### Added

- 05, 06, 09: publication as implemented in T15:
  - the pointer, with the review gate, partial acknowledgment, ETag concurrency, typed public confirmation and rollback;
  - members-only private site addresses;
  - suspension triggers on revoked connections and repositories;
  - published-site delivery through the resolver on every request;
  - the error codes `PUBLICATION_BLOCKED`, `ACKNOWLEDGMENT_REQUIRED`, `CONFIRMATION_REQUIRED` and `SLUG_INVALID`.

## [0.5.7] - 2026-11-18

### Added

- 05, 06: build-scoped search (`search_documents`, `GET /builds/{bid}/search`) with ranking by exact name, name prefix and weighted text, and relations carrying file IDs and lines.
- 02: the M3 code view: an accessible line renderer with deep links, hover preview, pin, Esc and focus return, the build tabs (Overview, Code, Guide, Search, Review), and admin waivers in Review. Adopting CodeMirror 6 is recorded as open.

## [0.5.6] - 2026-11-18

### Security

- 06: an event-stream slot was released only by the response body's generator, which never runs when the client disconnects before the first byte. Repeated aborted requests could leak slots until a workspace's members could no longer stream. Release is now idempotent and tied to the response lifecycle. Found by automated security review of T13.

## [0.5.5] - 2026-11-18

### Added

- 06: the build event stream as implemented in T13, negotiated on `Accept` beside the JSON polling form, with the `access.revoked` and `reset` events and the `STREAM_LIMIT` error.

## [0.5.4] - 2026-11-18

### Added

- 07: the evaluation corpus, labels, harness and committed replay baseline as implemented in M2, with their hard gates and their provisional status until two maintainers label them.

### Changed

- 07, 23: a preset may not pair a model writer with the replay checker, because the replay checker only matches identifiers and the baseline shows it labels unsupported claims as supported.

## [0.5.3] - 2026-11-18

### Changed

- 06: cancellation, retry and build-event polling as implemented in M2: immediate stop when nothing runs, no new earmark after a cancel request, cancellation at the next stage boundary, `retry_of_id`, `ESTIMATE_MISMATCH`, and `{items,next_after}` polling ahead of the SSE stream.

## [0.5.2] - 2026-11-18

### Added

- `contracts/claim-check.schema.json` and `claim-repair.schema.json` 1.0.0, the outputs of P05 and P06 (the files first shipped in 0.5.1 ahead of their implementation).
- 05: the full `claim_checks` row and `guide_pages.original_markdown_key`.
- 06: the quality summary and check list, waiver rules (`WAIVER_NOT_ALLOWED`), and claims on guide pages.

### Changed

- 07: verification as implemented in T12: the verify and check stages, checker batching and isolation, `unchecked` on any checker failure, the bounded repair cycle with results carried over for unchanged claims, and the `uncited_pages` blocker.
- 19: prompt set 0.3.0 adds P05 and P06.

## [0.5.1] - 2026-11-18

### Security

- 07: related context for explanations included definitions (name, kind, signature, path) from files outside the approved scope, which would be sent to the provider. It is now limited to references whose both ends are in scope; an integration test captures every prompt. Found by automated security review of T11.
- 19: the guide Markdown allowlist used regular expressions, which can disagree with a renderer (for example reference-style `javascript:` links). It now checks CommonMark tokens. Found by automated security review of T11.

## [0.5.0] - 2026-11-18

### Added

- `contracts/overview.schema.json`, `guide-plan.schema.json` and `guide-page.schema.json` 1.0.0: the structured outputs of P03, P01 and P04.
- `deployment/model-catalog.example.json`: an operator catalog template with one Anthropic preset, whose region and retention entries must be confirmed before use.
- 05: `generation_cache` (tenant-local cache and stage checkpoint), `file_explanations`, and the full `guide_pages` columns.
- 06: the guide index and page responses, file explanations in the file view, and 422 `PRESET_UNAVAILABLE`.

### Changed

- 07: the provider gateway as implemented in T11: the adapters, error classes, earmark bound, stage list, at most four units in flight, cache checkpoints, labeled fallbacks, and unknown outcomes on re-claim.
- 19: prompt set 0.2.0, its data-boundary markers and the repair template.

### Removed

- 22: the `MODEL_PROVIDER`, `MODEL_ID` and `MODEL_API_KEY` settings, replaced by `MODEL_CATALOG_FILE` and per-provider credentials (`ANTHROPIC_API_KEY`).

## [0.4.2] - 2026-11-18

### Security

- 07, 06: the bounded glob matcher of 0.4.1 was still polynomial in tenant-controlled paths and globs; a probe of the committed code took 61 s for one selection. Selection now has a fixed work budget (422 `SCOPE_TOO_COMPLEX`), segments match literal pieces with `str.find` and are memoized, `?` is no longer accepted, globs are limited to 20 per list with at most 8 `*` and 16 segments, and the estimate runs off the API event loop. Found by automated security review of 0.4.1.

## [0.4.1] - 2026-11-18

### Security

- 07: scope globs are matched by a bounded segment matcher instead of `PurePath.full_match`, whose regular-expression translation took exponential time on globs such as `*a*a*a*a*b`; a single estimate request could pin an API worker. Character classes are no longer accepted. Found by automated security review of T10.
- 06: snapshot acquisitions are admitted under the usage-account lock, at most `MAX_ACTIVE_ACQUISITIONS_PER_WORKSPACE` at once (409 `ACQUISITION_LIMIT`). The build limit did not cover them, so a workspace could start unbounded archive downloads and scans. Found by automated security review of T10.

## [0.4.0] - 2026-11-18

### Changed

- `contracts/task-envelope.schema.json` 2.0.0 (incompatible): a task names exactly one build or one snapshot, because a snapshot is now acquired and scanned before any build exists. Consumers still accept v1 messages while they drain (23).
- 05: `build_tasks` may belong to a snapshot; `builds` record the usage account their envelope was reserved against; `provider_attempts` gain the build, fencing generation, role, purpose, model and output digest; `usage_entries` amounts are non-negative, with the direction carried by the kind.
- 06: the snapshot, estimate, build start, usage and catalog-preset contracts as implemented in T10, with the error codes `SNAPSHOT_NOT_READY`, `PRESET_UNKNOWN`, `SCOPE_EMPTY`, `ESTIMATE_STALE`, `ALLOWANCE_EXCEEDED` and `WORKSPACE_BUILD_LIMIT`.
- 07: the estimate runs before parsing, so scope selection is one deterministic file-level function shared by the estimate and the build.
- 11: until entitlements exist, the monthly allowance is the operator setting `GENERATION_ALLOWANCE_MICRO_USD` (default 0).

## [0.3.12] - 2026-11-18

### Security

- 22: the parser sandbox adds a Landlock filesystem allowlist, denies process inspection (`ptrace`, `process_vm_*`) and program execution, and the worker becomes non-dumpable. A compromised parser could otherwise read credentials as the same UID and leak them through parse output. Found by automated security review. While implementing this, a jump-offset error in the rewritten seccomp program briefly allowed every denied syscall; the Linux probe caught it, and a unit test now pins the jump targets.

## [0.3.11] - 2026-11-18

### Changed

- M1 implementation decisions recorded:
  - 22: the sandbox design (seccomp bootstrap, worker self-check, Linux CI gate) and the dead-letter queue `codehover.dead`;
  - 07: the replay generator and the M1 stages;
  - 06: replay build start by `ref` until estimates arrive in M2;
  - 05: the webhook status function and re-notification via `notified_at`.

## [0.3.10] - 2026-11-18

### Security

- 09: introducing repository grants fails closed. Rows that existed before migration 0004, possibly with over-broad grants, start revoked until a reconnect. Found by automated security review.

## [0.3.9] - 2026-11-18

### Security

- 09, 05, 06: a GitHub connection grants only repositories the connecting user can read, with a known re-verification limitation. Previously anyone able to read one repository of an installation could expose all of its repositories to their workspace. Found by automated security review of T05; implemented by migration 0004.

## [0.3.8] - 2026-11-18

### Changed

- 06, 09, 22: the GitHub App connect flow as implemented in T05: the two-phase setup and callback, the required linked GitHub identity, the role re-check, the App URL and permission settings, the repository and ref contract, and the error codes.

## [0.3.7] - 2026-11-18

### Added

- 05: `invitation_by_token`, and the database-enforced owner invariant (deferred, workspace-locking trigger `memberships_keep_owner`). Implemented by migration 0003 (T04).
- 06: workspace, member and invitation contract details: ETag preconditions, invitation `accept_url` with the token in the fragment, role limits per action, and error codes.

### Changed

- 05: queries must filter by `workspace_id` explicitly even inside a workspace context, because user-scoped policies widen visibility. Found when an unfiltered member list leaked memberships across workspaces during T04.

## [0.3.6] - 2026-11-17

### Security

- 09 and 05: every OAuth flow is bound to the browser that started it, through a flow cookie whose digest consumption requires. This closes login CSRF: a callback URL opened in another browser can no longer sign the victim into the attacker's account, nor burn the attacker's transaction. Found by automated security review of T03.

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
