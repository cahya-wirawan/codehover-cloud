# Relational data model

## Conventions and entity ownership

All primary keys use PostgreSQL `uuid DEFAULT uuidv7()`, including events, attempts and ledgers. Provider IDs use text or bigint as appropriate and never replace internal UUIDs. UUIDv7 timestamps are not secrets or authorization. `created_at`, `updated_at` use `timestamptz`; UTC storage, locale formatting in UI. Money uses integer USD micro-units (`bigint`), never float; customer billing currency and minor units are stored separately. Prices and entitlements are versioned snapshots. Hashes use lowercase SHA-256 hex with length checks. JSONB is bounded configuration/metadata, not a substitute for core relational ownership.

Every tenant-owned entity carries `workspace_id NOT NULL`. Composite foreign keys include workspace ID to prevent accidental cross-tenant joins. Add `UNIQUE(workspace_id,id)` to referenced parents. Application authorization checks membership; RLS with FORCE ROW LEVEL SECURITY is defense in depth. Runtime role is neither superuser nor BYPASSRLS nor owner. Set `SET LOCAL app.workspace_id` per transaction after deriving authorized workspace from session; never from unvalidated request data. Workers derive tenancy from claimed task metadata and verify all children match. Missing setting denies rows; connection-pool reuse cannot leak prior settings. Identity/billing webhook tables use narrowly scoped services because no request workspace is initially available.

## Entities

| Table | Principal fields in addition to id/timestamps | Constraints/indexes and retention |
|---|---|---|
| users | display_name text, primary_email text nullable, status text, deleted_at | Identity subject is authority; unique email not used to merge; account deletion |
| oauth_identities | user_id FK, provider text, issuer text, subject text, verified_email text nullable | UNIQUE(provider,issuer,subject); no browser tokens |
| sessions | user_id FK, token_hash bytea, csrf_hash bytea, expires_at, last_seen_at, revoked_at | UNIQUE token_hash, expiry index; 30-day absolute, 7-day idle |
| oauth_transactions | state_hash bytea, provider, nonce_hash, encrypted_pkce_verifier, return_path, expires_at, consumed_at | One-time state, 10-minute TTL; includes link/login purpose and initiating session |
| workspaces | name varchar(80), slug varchar(48), status, deleted_at | UNIQUE slug; tombstone before purge |
| memberships | workspace_id, user_id, role | UNIQUE(workspace_id,user_id); owner invariant service transaction lock |
| invitations | workspace_id, email_normalized, role, token_hash, expires_at, accepted_at | 7-day TTL; unique live invitation by workspace/email |
| repository_connections | workspace_id, installation_id bigint, github_account_id bigint, connected_by, status, last_verified_at | UNIQUE(workspace_id,installation_id); revocation indexed |
| repositories | workspace_id, connection_id, provider_repo_id bigint, owner, name, visibility, default_branch | UNIQUE(workspace_id,provider_repo_id); names can change |
| projects | workspace_id, repository_id, name, slug, tracked_ref, config jsonb, config_version int, deleted_at | UNIQUE(workspace_id,slug); config ETag/version |
| snapshots | workspace_id, project_id, commit_sha text, archive_key, digest, observed_ref, status, source_bytes | UNIQUE(project_id,commit_sha); immutable |
| source_files | workspace_id, snapshot_id, path text, language, sha256, blob_key, line_count int, byte_count bigint, scan_status/reason | UNIQUE(snapshot_id,path); binary/excluded metadata contains no contents |
| estimates | workspace_id, project_id, snapshot_id, config_digest, price_version, low/high_micro_usd, token_estimate jsonb, expires_at | Immutable; 30-minute validity; 30-day metadata retention |
| builds | workspace_id, project_id, snapshot_id, estimate_id, retry_of_id nullable, status, config jsonb, config_digest, engine_version, prompt_version, cap_micro_usd, cancel_requested_at, manifest_key | Status index(project,status); one active project build partial unique index |
| build_tasks | workspace_id, build_id, stage, variant, work_key, state, attempt_count, lease_generation int, lease_until, heartbeat_at, next_attempt_at, output_key, error_code | UNIQUE(build_id,stage,variant,work_key); state/next_attempt index |
| provider_attempts | workspace_id, task_id, attempt_number, request_digest, provider_request_id, state, input/output/cache_tokens bigint, price_snapshot jsonb, max_reserved_micro_usd, settled_micro_usd nullable | UNIQUE(task_id,attempt_number); unknown cost explicitly nullable |
| build_events | workspace_id, build_id, sequence bigint, event_type, payload jsonb | UNIQUE(build_id,sequence); cursor indexed, 30 days |
| symbols | workspace_id, build_id, source_file_id, symbol_key text, name, kind, parent_key, start/end_line, start/end_column, signature, card jsonb | UNIQUE(build_id,symbol_key); build/name and build/path indexes |
| reference_edges | workspace_id, build_id, from_symbol_id nullable, to_symbol_id, source_file_id, line, column, resolution enum-like text, ambiguity_group nullable | Indexed(build_id,to_symbol_id); resolved index/name/unresolved distinct |
| guide_pages | workspace_id, build_id, slug, title, ordinal int, markdown_key, check_status | UNIQUE(build_id,slug); source citations separate |
| claim_checks | workspace_id, build_id, guide_page_id, claim_key, path, start/end_line, source_hash, status, reason, checker_model, waived_by/reason nullable | UNIQUE(guide_page_id,claim_key,path,start_line,end_line); status index |
| artifacts | workspace_id, build_id, kind, variant, object_key, sha256, byte_count, content_type, status, expires_at | UNIQUE(build_id,kind,variant); exports distinguished |
| publications | workspace_id, project_id, build_id, visibility, public_slug nullable, suspended_at, version int, published_by | UNIQUE project_id; UNIQUE public_slug; same-project/build FK |
| outbox | workspace_id nullable, aggregate_id, event_type, payload jsonb, available_at, published_at, dispatch_attempts | unpublished due index; 7-day post-dispatch retention |
| usage_accounts | workspace_id, period_start/end, allowance_micro_usd, reserved_micro_usd, settled_micro_usd, version | UNIQUE(workspace_id,period_start); row locked for reservations |
| usage_entries | workspace_id, account_id, build_id nullable, attempt_id nullable, kind, amount_micro_usd bigint, idempotency_key | UNIQUE(workspace_id,idempotency_key); append-only, reversal entries |
| billing_customers | workspace_id, provider_customer_id, billing_email, currency | UNIQUE workspace and provider customer |
| subscriptions | workspace_id, provider_subscription_id, status, plan_key, entitlement_version, period_end, cancel_at_period_end | UNIQUE provider subscription; authoritative reconciliation snapshot |
| webhook_receipts | provider, provider_event_id, payload_digest, received_at, processed_at, state, encrypted_payload_key nullable | UNIQUE(provider,provider_event_id); narrow service access; 30-day payload TTL |
| audit_events | workspace_id nullable, actor_user_id nullable, action, resource_type/id, metadata jsonb, request_id | Append-only; tenant/date index; 180 days, no source |
| deletion_requests | workspace_id nullable, user_id nullable, scope, requested_at, state, completed_at, error_code | Retriable purge cursor, minimal identifier tombstone |

Search documents are tenant/build-scoped derived rows for definition/file/guide text with weighted tsvector + GIN, normalized identifier tokens and pg_trgm optional GIN/GiST on names. Code blobs stay in object storage; no global source index/cache. FTS language config is `simple` for identifiers; localized prose configurations are added with language variants. Search always filters workspace/build before ranking and neighbor expansion.

## Core SQL design reference

See `contracts/core_schema.sql` for executable DDL illustrating projects/builds/tasks/outbox/usage constraints and RLS. It is a design reference, not a complete migration for all tables. Alembic revisions must implement the complete table inventory, creation order, constraints, policies and indexes. Never use ORM `create_all` as deployment migration.

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

Latest published build retained while active; latest 5 unpublished ready builds retained 30 days by default. Failed snapshots/artifacts 7 days; scratch 24h maximum or immediately on completion; exports 24h; model cache 30 days within workspace only; sanitized task/events 30 days; audit 180 days; billing records per configured statutory policy, never arbitrary source retention. Source deletion target ≤24h in active stores; backup copies age out ≤35 days with no routine restoration access. Restore must reapply deletion tombstones before serving traffic.

Migration gates: PostgreSQL 18 clean install; upgrade from preceding release; UUID version assertions; FK/RLS negative tests under runtime role; index explain plans; concurrent reservation/publish tests. Large new indexes use appropriate online migration outside a transaction where required. Backfills are resumable jobs, not long startup migrations.

Budget counters follow the single-envelope model in document 11: build start holds cap once in workspace reserved; provider_attempts earmark portions of that hold; actual settlement transfers cost from reserved to settled; terminal cleanup releases the remainder except unknown-outcome holds. Runtime migrations revoke UPDATE/DELETE on append-only usage_entries and audit_events despite broad local bootstrap default grants; controlled purge uses a separate audited maintenance role.
