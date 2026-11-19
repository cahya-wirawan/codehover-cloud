-- 0026 bring your own key and batch processing (M6, T24; 07 "BYOK", "optional submit_batch/get_batch").

-- One live key per workspace and provider, sealed (AES-256-GCM, associated data = workspace and provider)
-- and shown only as a hint. Replacing or removing a key wipes the sealed value.
CREATE TABLE provider_keys (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  provider text NOT NULL CHECK (provider IN ('anthropic')),
  sealed_key bytea,
  hint text NOT NULL CHECK (char_length(hint) <= 40),
  created_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  verified_at timestamptz,
  revoked_at timestamptz,
  CHECK ((revoked_at IS NULL) = (sealed_key IS NOT NULL)),
  UNIQUE (workspace_id, id)
);
CREATE UNIQUE INDEX uq_provider_keys_live ON provider_keys(workspace_id, provider) WHERE revoked_at IS NULL;

-- A provider batch of one stage: its items map each custom_id (the attempt) to the cache key it fills.
CREATE TABLE provider_batches (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  task_id uuid NOT NULL,
  stage text NOT NULL CHECK (stage ~ '^[a-z]{1,32}$'),
  provider text NOT NULL CHECK (provider IN ('anthropic')),
  provider_batch_id text CHECK (provider_batch_id IS NULL OR provider_batch_id ~ '^[A-Za-z0-9_-]{1,128}$'),
  state text NOT NULL DEFAULT 'submitting' CHECK (state IN ('submitting', 'in_progress', 'completed', 'abandoned')),
  items jsonb NOT NULL CHECK (jsonb_typeof(items) = 'array' AND jsonb_array_length(items) <= 10000),
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CHECK ((state = 'in_progress') <= (provider_batch_id IS NOT NULL)),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id),
  FOREIGN KEY (workspace_id, task_id) REFERENCES build_tasks(workspace_id, id)
);
CREATE INDEX ix_provider_batches_build ON provider_batches(workspace_id, build_id, stage, created_at DESC);

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['provider_keys', 'provider_batches'] LOOP
    EXECUTE format('CREATE POLICY tenant ON %I TO app_runtime
      USING (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)
      WITH CHECK (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON %I FROM app_runtime', t);
  END LOOP;
END $$;
GRANT SELECT, INSERT ON provider_keys TO app_runtime;
GRANT UPDATE (sealed_key, revoked_at, verified_at) ON provider_keys TO app_runtime;
GRANT SELECT, INSERT, UPDATE ON provider_batches TO app_runtime;

-- Attempts sent in a batch are `batched` until the batch ends: a re-claimed task must not mistake them
-- for interrupted calls, and they keep their holds like started ones.
ALTER TABLE provider_attempts DROP CONSTRAINT provider_attempts_state_check;
ALTER TABLE provider_attempts ADD CONSTRAINT provider_attempts_state_check
  CHECK (state IN ('started', 'batched', 'succeeded', 'failed', 'unknown', 'reconciled'));
ALTER TABLE provider_attempts DROP CONSTRAINT provider_attempts_check;
ALTER TABLE provider_attempts ADD CONSTRAINT provider_attempts_check
  CHECK ((state IN ('started', 'batched', 'unknown')) = (settled_micro_usd IS NULL));
DROP INDEX ix_provider_attempts_open;
CREATE INDEX ix_provider_attempts_open ON provider_attempts(build_id) WHERE state IN ('started', 'batched', 'unknown');

-- A capped build reserves against an allowance account, except an own-key build: the provider bills the
-- workspace's own key, so its envelope lives on the build alone.
ALTER TABLE builds DROP CONSTRAINT builds_capped_has_account;
ALTER TABLE builds ADD CONSTRAINT builds_capped_has_account
  CHECK (cap_micro_usd = 0 OR usage_account_id IS NOT NULL OR config->>'funding' = 'own_key') NOT VALID;
