-- 0006 snapshot acquisition tasks, estimates and single-envelope build reservations (backlog T10; 05, 07, 11).

-- ---------------------------------------------------------------- snapshot tasks
-- A snapshot is acquired and scanned before any build exists (POST /projects/{pid}/snapshots), so the
-- estimate can see its file list. Such tasks belong to the snapshot instead of a build; exactly one owner.
ALTER TABLE build_tasks ALTER COLUMN build_id DROP NOT NULL;
ALTER TABLE build_tasks ADD COLUMN snapshot_id uuid;
ALTER TABLE build_tasks ADD CONSTRAINT build_tasks_one_owner CHECK (num_nonnulls(build_id, snapshot_id) = 1);
ALTER TABLE build_tasks ADD CONSTRAINT build_tasks_snapshot_fk
  FOREIGN KEY (workspace_id, snapshot_id) REFERENCES snapshots(workspace_id, id);
-- At most one unfinished acquisition per snapshot; finished ones stay as history.
CREATE UNIQUE INDEX uq_build_tasks_snapshot_live ON build_tasks(snapshot_id)
  WHERE snapshot_id IS NOT NULL AND state IN ('pending', 'leased', 'retry_wait');
GRANT SELECT (snapshot_id) ON build_tasks TO app_system;

-- ---------------------------------------------------------------- estimates
ALTER TABLE estimates ADD COLUMN created_by uuid REFERENCES users(id);
ALTER TABLE estimates ADD CONSTRAINT estimates_token_estimate_object CHECK (jsonb_typeof(token_estimate) = 'object');
ALTER TABLE estimates ADD CONSTRAINT estimates_price_version CHECK (price_version ~ '^catalog-[0-9]{4}-[0-9]{2}-[a-z]$');

-- ---------------------------------------------------------------- reservations (11)
-- The build envelope is reserved once against the usage account of the period it started in; later
-- settlement and release always go to that account, even after period rollover.
ALTER TABLE builds ADD COLUMN usage_account_id uuid;
ALTER TABLE builds ADD CONSTRAINT builds_usage_account_fk
  FOREIGN KEY (workspace_id, usage_account_id) REFERENCES usage_accounts(workspace_id, id);
-- NOT VALID: enforced for every new or updated row. Builds created through the API before 0006 always had
-- a zero cap; only hand-inserted rows (test fixtures) can predate the rule.
ALTER TABLE builds ADD CONSTRAINT builds_capped_has_account
  CHECK (cap_micro_usd = 0 OR usage_account_id IS NOT NULL) NOT VALID;
ALTER TABLE builds ADD CONSTRAINT builds_capped_has_estimate
  CHECK (cap_micro_usd = 0 OR estimate_id IS NOT NULL) NOT VALID;
CREATE INDEX ix_builds_workspace_active ON builds(workspace_id) WHERE status IN ('queued', 'running');

-- Ledger rows name what they move; the idempotency key makes every transition apply once.
ALTER TABLE usage_entries ADD CONSTRAINT usage_entries_amount_nonnegative CHECK (amount_micro_usd >= 0);
ALTER TABLE usage_entries ADD CONSTRAINT usage_entries_key_length CHECK (char_length(idempotency_key) BETWEEN 8 AND 200);
CREATE INDEX ix_usage_entries_account ON usage_entries(workspace_id, account_id, created_at, id);

-- ---------------------------------------------------------------- provider attempts (07, 11)
-- One row per outbound model request. Before the request, its maximum cost is earmarked inside the build
-- envelope (state started); settlement records actual usage once. An attempt whose outcome is uncertain
-- (timeout, crash, lost lease) becomes unknown and keeps its conservative hold until reconciled.
CREATE TABLE provider_attempts (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  task_id uuid NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number >= 1),
  lease_generation integer NOT NULL CHECK (lease_generation >= 1),
  role text NOT NULL CHECK (role IN ('writer', 'checker')),
  purpose text NOT NULL CHECK (purpose ~ '^[a-z][a-z_]{0,31}$'),
  model_id text NOT NULL,
  request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
  provider_request_id text CHECK (provider_request_id IS NULL OR char_length(provider_request_id) <= 200),
  state text NOT NULL DEFAULT 'started' CHECK (state IN ('started', 'succeeded', 'failed', 'unknown', 'reconciled')),
  input_tokens bigint CHECK (input_tokens IS NULL OR input_tokens >= 0),
  output_tokens bigint CHECK (output_tokens IS NULL OR output_tokens >= 0),
  cache_read_tokens bigint CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
  cache_write_tokens bigint CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
  price_snapshot jsonb NOT NULL CHECK (jsonb_typeof(price_snapshot) = 'object'),
  max_reserved_micro_usd bigint NOT NULL CHECK (max_reserved_micro_usd >= 0),
  settled_micro_usd bigint CHECK (settled_micro_usd IS NULL OR settled_micro_usd >= 0),
  error_code text,
  output_digest text CHECK (output_digest IS NULL OR output_digest ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CHECK ((state IN ('started', 'unknown')) = (settled_micro_usd IS NULL)),
  UNIQUE (task_id, attempt_number),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id),
  FOREIGN KEY (workspace_id, task_id) REFERENCES build_tasks(workspace_id, id)
);
CREATE INDEX ix_provider_attempts_open ON provider_attempts(build_id) WHERE state IN ('started', 'unknown');

ALTER TABLE usage_entries ADD COLUMN attempt_id uuid;
ALTER TABLE usage_entries ADD CONSTRAINT usage_entries_attempt_fk
  FOREIGN KEY (workspace_id, attempt_id) REFERENCES provider_attempts(workspace_id, id);

GRANT SELECT, INSERT, UPDATE ON provider_attempts TO app_runtime;
CREATE POLICY tenant ON provider_attempts TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE provider_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_attempts FORCE ROW LEVEL SECURITY;
