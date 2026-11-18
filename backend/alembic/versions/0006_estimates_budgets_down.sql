ALTER TABLE usage_entries DROP CONSTRAINT IF EXISTS usage_entries_attempt_fk;
ALTER TABLE usage_entries DROP COLUMN IF EXISTS attempt_id;
DROP TABLE IF EXISTS provider_attempts;

DROP INDEX IF EXISTS ix_usage_entries_account;
ALTER TABLE usage_entries DROP CONSTRAINT IF EXISTS usage_entries_key_length;
ALTER TABLE usage_entries DROP CONSTRAINT IF EXISTS usage_entries_amount_nonnegative;

DROP INDEX IF EXISTS ix_builds_workspace_active;
ALTER TABLE builds DROP CONSTRAINT IF EXISTS builds_capped_has_estimate;
ALTER TABLE builds DROP CONSTRAINT IF EXISTS builds_capped_has_account;
ALTER TABLE builds DROP CONSTRAINT IF EXISTS builds_usage_account_fk;
ALTER TABLE builds DROP COLUMN IF EXISTS usage_account_id;

ALTER TABLE estimates DROP CONSTRAINT IF EXISTS estimates_price_version;
ALTER TABLE estimates DROP CONSTRAINT IF EXISTS estimates_token_estimate_object;
ALTER TABLE estimates DROP COLUMN IF EXISTS created_by;

REVOKE SELECT (snapshot_id) ON build_tasks FROM app_system;
DROP INDEX IF EXISTS uq_build_tasks_snapshot_live;
DELETE FROM build_tasks WHERE snapshot_id IS NOT NULL;
ALTER TABLE build_tasks DROP CONSTRAINT IF EXISTS build_tasks_snapshot_fk;
ALTER TABLE build_tasks DROP CONSTRAINT IF EXISTS build_tasks_one_owner;
ALTER TABLE build_tasks DROP COLUMN IF EXISTS snapshot_id;
ALTER TABLE build_tasks ALTER COLUMN build_id SET NOT NULL;
