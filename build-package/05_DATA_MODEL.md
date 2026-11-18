# Relational data model

## Conventions and entity ownership

All primary keys use PostgreSQL `uuid DEFAULT uuidv7()`, including events, attempts and ledgers. Provider IDs use text or bigint as appropriate and never replace internal UUIDs. UUIDv7 timestamps are not secrets or authorization. `created_at`, `updated_at` use `timestamptz`; UTC storage, locale formatting in UI. Money uses integer USD micro-units (`bigint`), never float; customer billing currency and minor units are stored separately. Prices and entitlements are versioned snapshots. Hashes use lowercase SHA-256 hex with length checks. JSONB is bounded configuration/metadata, not a substitute for core relational ownership.

Every tenant-owned entity carries `workspace_id NOT NULL`. Composite foreign keys include workspace ID to prevent accidental cross-tenant joins. Add `UNIQUE(workspace_id,id)` to referenced parents. Application authorization checks membership; RLS with FORCE ROW LEVEL SECURITY is defense in depth. Runtime role is neither superuser nor BYPASSRLS nor owner. Set the tenant context per transaction with bound parameters, `SELECT set_config('app.workspace_id', $1, true)` (transaction-local, equivalent to `SET LOCAL`; `SET` cannot take bind parameters, so never string-format it), after deriving the authorized workspace from the session; never from unvalidated request data. Missing setting denies rows; connection-pool reuse cannot leak prior settings. Identity/billing webhook tables use narrowly scoped services because no request workspace is initially available.

## Tenant context resolution and database roles

FORCE RLS on every tenant table means a query without a valid context sees nothing. Four access paths need an explicit design rather than an ad hoc bypass; `contracts/core_schema.sql` implements each and `contracts/core_schema_checks.sql` asserts them under the real roles.

| Path | Mechanism |
|---|---|
| Flat routes (`/projects/{pid}`, `/builds/{bid}`, `/exports/{eid}`) | `app_private.resource_workspace(kind, id)` returns only the owning workspace ID (NULL for unknown kind/id). The API then checks membership and role, sets `app.workspace_id`, and loads the resource under RLS. Not a member → generic 404. |
| User-scoped lists (`GET /workspaces`, workspace switcher, `/me`) | `app.user_id` is set from the session; SELECT-only policies expose the user's own membership rows and their workspaces. Writes still require the workspace context. |
| Workspace creation | `app_private.create_workspace(name, slug)` inserts the workspace and owner membership atomically using `app.user_id`; the new ID does not exist before the call, so a context cannot be set first. |
| Sign-in and sessions (before any context exists) | `app_private.identity_user(provider, issuer, subject)`, `create_user_with_identity(...)`, `session_by_token(token_hash)` (live, unrevoked sessions of active users only) and `oauth_transaction_create/consume`. The OAuth state is consumed exactly once, before expiry, for the same provider. A TTL outside (0, 10 minutes] is rejected. The runtime role has no direct access to `oauth_transactions` and cannot INSERT users. Sessions, identities and idempotency keys are visible only to their own `app.user_id`. |
| Published sites (`/published/{slug}`) | `app_private.resolve_publication(slug)` returns workspace/project/build/visibility for an unsuspended publication; delivery then reads under that workspace context. |
| Cross-tenant control loops (dispatcher, scheduler, lease reconciler, GC) | Separate login role `app_system` with role-scoped `USING (true)` policies and column-level grants on `outbox` and the scheduling columns of `build_tasks` only. It cannot read source, prose, outputs, ledgers or any other tenant table. |

Queries always filter by `workspace_id` explicitly, even inside a workspace context. RLS is defense in depth, and the user-scoped SELECT policies (own memberships and workspaces, needed for the switcher) legitimately widen what a context shows. During implementation, an unfiltered member list leaked the caller's memberships in other workspaces.

Invitations are accepted through `app_private.invitation_by_token(token_hash)`; the workspace comes from the validated token, never from request data. The owner invariant is enforced in the database by the deferred constraint trigger `memberships_keep_owner`. It runs at commit, locks the workspace row and re-counts owners, so concurrent removals of the last two owners serialize and one fails. Workspaces in `deleting` status are exempt for purge.

Signed GitHub webhooks (no context) change connection status only through `app_private.set_installation_status`, which never revives a revoked connection. `build_tasks.notified_at` lets the reconciler re-notify runnable tasks whose notification was lost; expiring a dead lease clears it.

Lookup functions are `SECURITY DEFINER` with a fixed `search_path`, owned by the NOLOGIN role `app_resolver`, which can read only `id`/`workspace_id`/lifecycle columns through its own policies. No role has BYPASSRLS. EXECUTE is revoked from PUBLIC and granted to `app_runtime`. Task workers stay on `app_runtime`: they set `app.workspace_id` from the message, then claim the task by ID; a mismatched workspace makes the row invisible, so the claim fails closed. Purge and audited reconciliation use the separate maintenance role. Role bootstrap lives in `deployment/init-db.sh`; migrations grant privileges but never create login roles.

## Entities

| Table | Principal fields in addition to id/timestamps | Constraints/indexes and retention |
|---|---|---|
| users | display_name text, primary_email text nullable, status text, deleted_at | Identity subject is authority; unique email not used to merge; account deletion |
| oauth_identities | user_id FK, provider text, issuer text, subject text, verified_email text nullable | UNIQUE(provider,issuer,subject); no browser tokens |
| sessions | user_id FK, token_hash bytea, csrf_hash bytea, expires_at, last_seen_at, revoked_at | UNIQUE token_hash, expiry index; 30-day absolute, 7-day idle |
| oauth_transactions | state_hash bytea, browser_binding_hash bytea, provider, nonce_hash, encrypted_pkce_verifier, return_path, expires_at, consumed_at | One-time state, 10-minute TTL; includes link/login purpose and initiating session. `encrypted_pkce_verifier` holds the AES-GCM-sealed PKCE verifier and OIDC nonce, bound to the state digest as associated data; `nonce_hash` cross-checks the nonce |
| workspaces | name varchar(80), slug varchar(48), status, deleted_at | UNIQUE slug; tombstone before purge |
| memberships | workspace_id, user_id, role | UNIQUE(workspace_id,user_id); owner invariant service transaction lock |
| invitations | workspace_id, email_normalized, role, token_hash, expires_at, accepted_at | 7-day TTL; unique live invitation by workspace/email |
| repository_connections | workspace_id, installation_id bigint, github_account_id bigint, connected_by, status, last_verified_at | UNIQUE(workspace_id,installation_id); revocation indexed |
| repositories | workspace_id, connection_id, provider_repo_id bigint, owner, name, visibility, default_branch, access_revoked_at | UNIQUE(workspace_id,provider_repo_id); names can change; only rows with `access_revoked_at` NULL are granted (09) |
| projects | workspace_id, repository_id, name, slug, tracked_ref, config jsonb, config_version int, last_observed_sha/last_observed_at (freshness), deleted_at | UNIQUE(workspace_id,slug); config ETag/version |
| snapshots | workspace_id, project_id, commit_sha text, archive_key, digest, observed_ref, status, source_bytes | UNIQUE(project_id,commit_sha); immutable |
| source_files | workspace_id, snapshot_id, path text, language, sha256, blob_key, line_count int, byte_count bigint, scan_status/reason | UNIQUE(snapshot_id,path); binary/excluded metadata contains no contents |
| estimates | workspace_id, project_id, snapshot_id, config_digest, price_version, low/high_micro_usd, token_estimate jsonb, expires_at, created_by | Immutable; 30-minute validity; 30-day metadata retention; `price_version` is a catalog revision (23) |
| builds | workspace_id, project_id, snapshot_id, estimate_id, retry_of_id nullable, status, config jsonb, config_digest, engine_version, prompt_version (SemVer, document 23), cap_micro_usd, reserved/settled_micro_usd, usage_account_id, cancel_requested_at, manifest_key | Status index(project,status); one active project build partial unique index; a nonzero cap requires an estimate and the usage account it was reserved against |
| build_tasks | workspace_id, build_id nullable, snapshot_id nullable, stage, variant, work_key, state, attempt_count, lease_generation int, lease_until, heartbeat_at, next_attempt_at, notified_at, output_key, error_code | Exactly one owner: a build (pipeline stages) or a snapshot (acquisition before any build); UNIQUE(build_id,stage,variant,work_key); at most one unfinished task per snapshot; state/next_attempt index |
| provider_attempts | workspace_id, build_id, task_id, attempt_number, lease_generation, role (writer/checker), purpose, model_id, request_digest, provider_request_id, state (started/succeeded/failed/unknown/reconciled), input/output/cache_read/cache_write_tokens bigint, price_snapshot jsonb, max_reserved_micro_usd, settled_micro_usd nullable, output_digest | UNIQUE(task_id,attempt_number); settled cost is NULL exactly while started or unknown; open-attempt index per build |
| build_events | workspace_id, build_id, sequence bigint, event_type, payload jsonb | UNIQUE(build_id,sequence); cursor indexed, 30 days |
| symbols | workspace_id, build_id, source_file_id, symbol_key text, name, kind, parent_key, start/end_line (1-based inclusive), start/end_column (0-based, end-exclusive, Unicode scalar offsets), signature, card jsonb | UNIQUE(build_id,symbol_key); build/name and build/path indexes |
| reference_edges | workspace_id, build_id, from_symbol_id nullable, to_symbol_id, source_file_id, line, column, resolution enum-like text, ambiguity_group nullable | Indexed(build_id,to_symbol_id); resolved index/name/unresolved distinct |
| guide_pages | workspace_id, build_id, slug, title, ordinal int (1–8), purpose, source_ranges jsonb, status (planned/written/failed), markdown_key, original_markdown_key (text before the repair cycle), check_status, provenance jsonb | UNIQUE(build_id,slug), UNIQUE(build_id,ordinal); markdown_key set exactly when written; source citations separate |
| file_explanations | workspace_id, build_id, source_file_id, status (generated/partial/failed), summary, chunks jsonb (range, summary, notes, status, provenance per chunk), provenance | UNIQUE(build_id,source_file_id); the file card shows chunk summaries in source order |
| generation_cache | workspace_id, cache_key (SHA-256 of every input, 07), purpose, output jsonb ≤ 256 KiB, output_digest, model_id, prompt_version, attempt_id, expires_at | UNIQUE(workspace_id,cache_key); written in the fenced settlement transaction, so it is also the stage checkpoint; 30 days; never shared across workspaces |
| claim_checks | workspace_id, build_id, guide_page_id, claim_key, claim_text, ordinal, revision (0 original, 1 repaired), current, citation (the marker as written), source_file_id/path nullable, start/end_line, source_hash, deterministic (valid/invalid), status, reason, evidence_lines, checker_model, waived_by/reason/at nullable | UNIQUE(guide_page_id,claim_key,citation), keyed by the marker because an invalid citation may name no file; invalid exactly when deterministically invalid; only unsupported/partial rows can carry a waiver (reason 10–1000 chars); superseded rows stay with current = false; status index on current rows |
| artifacts | workspace_id, build_id, kind, variant, object_key, sha256, byte_count, content_type, status, expires_at | UNIQUE(build_id,kind,variant); exports distinguished |
| exports | workspace_id, build_id, format (static_zip/kb_json), variant, status (pending/ready/failed), requested_by, object_key, sha256, byte_count, error_code, completed_at, expires_at | Ready exactly when stored with an expiry (24 h); produced by a build task (stage `export`, work key = export id) on a ready build; a failed export never fails the build; resolvable for flat `/exports/{eid}` routes |
| publications | workspace_id, project_id, build_id, visibility, public_slug nullable, version int, acknowledged_partial, suspended_at/suspended_reason (connection_inactive, repository_revoked, operator), published_by | UNIQUE project_id; UNIQUE public_slug; same-project/build FK; public requires a slug; a private publication may have one (members-only site); triggers on `repository_connections.status` and `repositories.access_revoked_at` suspend affected publications at once (FR-027) |
| outbox | workspace_id nullable, aggregate_id, event_type, payload jsonb, available_at, published_at, dispatch_attempts | unpublished due index; 7-day post-dispatch retention |
| usage_accounts | workspace_id, period_start/end, allowance_micro_usd, reserved_micro_usd, settled_micro_usd, version | UNIQUE(workspace_id,period_start); row locked for reservations |
| usage_entries | workspace_id, account_id, build_id nullable, attempt_id nullable, kind (reserve/settle/release/adjustment/reversal), amount_micro_usd bigint ≥ 0, idempotency_key | UNIQUE(workspace_id,idempotency_key); append-only, reversal entries; the kind carries the direction |
| billing_customers | workspace_id, provider_customer_id, billing_email, currency | UNIQUE workspace and provider customer |
| subscriptions | workspace_id, provider_subscription_id, status, plan_key, entitlement_version, period_end, cancel_at_period_end | UNIQUE provider subscription; authoritative reconciliation snapshot |
| webhook_receipts | provider, provider_event_id, payload_digest, received_at, processed_at, state, encrypted_payload_key nullable | UNIQUE(provider,provider_event_id); narrow service access; 30-day payload TTL |
| audit_events | workspace_id nullable, actor_user_id nullable, action, resource_type/id, metadata jsonb, request_id | Append-only; tenant/date index; 180 days, no source |
| idempotency_keys | user_id, workspace_id nullable, route, key (16–128 chars), request_digest, response_status, response_body jsonb, expires_at | UNIQUE(user_id,route,key); 24h retention; same key + different digest → 409 (06) |
| deletion_requests | scope (project, workspace, account), workspace_id/project_id/user_id by scope, requested_by, state (pending, purging, completed), not_before, lease_until, attempts, requested_at, completed_at, error_code | Minimal identifier tombstone and retriable purge cursor; one per target (partial unique indexes); due 5 minutes after the request, once fenced leases have expired; claimed by the purger through `app_private.claim_deletion` (identifiers and a lease only); a copy lives in object storage under `tombstones/` for restores (0015) |

M3 implements them as `search_documents` (kind symbol/file/guide, ref_id, name with generated `name_lower`, path, line, symbol kind, snippet, identifier `terms` split from snake_case and camelCase, body, and a generated weighted `document` tsvector), rebuilt by the render stage before the manifest, with FORCE RLS and a GIN index. Only text files that passed the scan are indexed. Search documents are tenant/build-scoped derived rows for definition/file/guide text with weighted tsvector + GIN, normalized identifier tokens and pg_trgm optional GIN/GiST on names. Code blobs stay in object storage; no global source index/cache. FTS language config is `simple` for identifiers; localized prose configurations are added with language variants. Search always filters workspace/build before ranking and neighbor expansion.

## Core SQL design reference

See `contracts/core_schema.sql` for executable DDL illustrating workspace/membership/project/build/task/publication/outbox/usage constraints, RLS, role-scoped policies and the resolver functions, and `contracts/core_schema_checks.sql` for the matching assertions (both executed on PostgreSQL 18.0). It is a design reference, not a complete migration for all tables. Alembic revisions must implement the complete table inventory, creation order, constraints, policies and indexes. Never use ORM `create_all` as deployment migration.

SQLAlchemy mapping convention:

```python
from uuid import UUID
from sqlalchemy import text, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                               server_default=text("uuidv7()"))
# Insert then flush/RETURNING before referring to a database-generated id.
# Alembic must preserve server defaults and tenant composite constraints.
```

## Build states and consistency

`queued → running → ready | failed | cancelled | budget_exhausted`; `cancel_requested` is a separate timestamp/flag while running settles work. Ready means manifest exists, all required tasks complete, verification execution finished, and quality report persisted. A ready draft with unsupported content still needs publication policy review. No terminal build returns to running; retry creates another build. Stage/task states: pending, leased, succeeded, skipped, retry_wait, failed, cancelled. Attempts and ledger entries are immutable after final settlement except audited reconciliation adjustments.

Publication constraints require the build belongs to the project and workspace, is ready, is not deletion-marked, and connection is valid. Since a simple FK cannot enforce build status, service transaction locks build/project/publication and checks readiness. Database constraints enforce ownership even for defective application paths. One active build/project is enforced by partial unique index, workspace concurrency by quota row lock. Cascading physical deletion is delayed until active tasks are fenced and publication removed.

## Retention and migrations

Latest published build retained while active; latest 5 unpublished ready builds retained 30 days by default. Failed snapshots/artifacts 7 days; scratch 24h maximum or immediately on completion; exports 24h; model cache 30 days within workspace only; sanitized task/events 30 days; audit 180 days; billing records per configured statutory policy, never arbitrary source retention. Deletion (0015) keeps the rows the ledger and audit log reference (projects, builds, tasks, snapshots, estimates, provider attempts) as minimal tombstones with names scrubbed, and removes content rows (source files, symbols, references, explanations, guide pages, claim checks, search documents, artifacts, events, exports, publications, generation cache) and every object under the project or workspace prefix. Project and workspace slugs are unique among live rows only, so a deleted address can be reused. Source deletion target ≤24h in active stores; backup copies age out ≤35 days with no routine restoration access. Restore must reapply deletion tombstones before serving traffic.

Migration gates: PostgreSQL 18 clean install; upgrade from preceding release; UUID version assertions; FK/RLS negative tests under runtime role; index explain plans; concurrent reservation/publish tests. Large new indexes use appropriate online migration outside a transaction where required. Backfills are resumable jobs, not long startup migrations.

Budget counters follow the single-envelope model in document 11: build start holds cap once in workspace reserved; provider_attempts earmark portions of that hold; actual settlement transfers cost from reserved to settled; terminal cleanup releases the remainder except unknown-outcome holds. Runtime migrations revoke UPDATE/DELETE on append-only usage_entries and audit_events despite broad local bootstrap default grants; controlled purge uses a separate audited maintenance role.
