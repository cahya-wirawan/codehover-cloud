UPDATE webhook_receipts SET state = 'failed' WHERE state = 'rejected';
ALTER TABLE webhook_receipts DROP CONSTRAINT webhook_receipts_state_check;
ALTER TABLE webhook_receipts ADD CONSTRAINT webhook_receipts_state_check
  CHECK (state IN ('received', 'processed', 'ignored', 'failed'));
ALTER TABLE webhook_receipts DROP COLUMN details;
UPDATE publications SET suspended_at = NULL, suspended_reason = NULL WHERE suspended_reason = 'billing';
ALTER TABLE publications DROP CONSTRAINT publications_suspended_reason_check;
ALTER TABLE publications ADD CONSTRAINT publications_suspended_reason_check
  CHECK (suspended_reason IS NULL OR suspended_reason IN ('connection_inactive', 'repository_revoked', 'operator'));
DROP FUNCTION app_private.start_trial(uuid, bigint, integer);
DROP FUNCTION app_private.billing_workspace(text);
DROP POLICY resolver_trial ON subscriptions;
DROP POLICY resolver_trial ON memberships;
REVOKE SELECT (workspace_id, user_id, role) ON memberships FROM app_resolver;
DROP INDEX uq_entitlement_trial;
ALTER TABLE entitlement_grants DROP COLUMN once;
ALTER TABLE entitlement_grants DROP COLUMN kind;
DROP TABLE subscriptions;
DROP TABLE billing_customers;
