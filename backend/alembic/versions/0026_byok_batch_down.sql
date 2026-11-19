ALTER TABLE builds DROP CONSTRAINT builds_capped_has_account;
ALTER TABLE builds ADD CONSTRAINT builds_capped_has_account
  CHECK (cap_micro_usd = 0 OR usage_account_id IS NOT NULL) NOT VALID;
UPDATE provider_attempts SET state = 'unknown' WHERE state = 'batched';
DROP INDEX ix_provider_attempts_open;
CREATE INDEX ix_provider_attempts_open ON provider_attempts(build_id) WHERE state IN ('started', 'unknown');
ALTER TABLE provider_attempts DROP CONSTRAINT provider_attempts_check;
ALTER TABLE provider_attempts ADD CONSTRAINT provider_attempts_check
  CHECK ((state IN ('started', 'unknown')) = (settled_micro_usd IS NULL));
ALTER TABLE provider_attempts DROP CONSTRAINT provider_attempts_state_check;
ALTER TABLE provider_attempts ADD CONSTRAINT provider_attempts_state_check
  CHECK (state IN ('started', 'succeeded', 'failed', 'unknown', 'reconciled'));
DROP TABLE provider_batches;
DROP TABLE provider_keys;
