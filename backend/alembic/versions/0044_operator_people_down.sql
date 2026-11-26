-- Back to 0043: no operator people view, suspensions, credits or member limits. Suspended people become
-- active again (their sessions and tokens stay ended); credits stay as manual grants.
DROP TABLE member_limits;

DROP FUNCTION app_private.ops_credit_owner(uuid, text);
UPDATE entitlement_grants SET kind = 'manual' WHERE kind = 'credit';
ALTER TABLE entitlement_grants DROP CONSTRAINT entitlement_grants_kind_check;
ALTER TABLE entitlement_grants ADD CONSTRAINT entitlement_grants_kind_check CHECK (kind IN ('manual', 'trial'));

DROP FUNCTION app_private.ops_owners(text, text, integer, text[]);
DROP FUNCTION app_private.ops_set_person_status(uuid, boolean, text);
DROP FUNCTION app_private.ops_person(uuid);
DROP FUNCTION app_private.ops_people(text, text, text, timestamptz, uuid, integer);
DROP FUNCTION app_private.person_month_spend(uuid);
DROP FUNCTION app_private.identity_suspended(text, text, text);
DROP FUNCTION app_private.user_active(uuid);

REVOKE SELECT (status, plan_key) ON subscriptions FROM app_resolver;
REVOKE SELECT (created_by, usage_account_id) ON builds FROM app_resolver;
REVOKE SELECT (user_id) ON mcp_sessions FROM app_resolver;
DROP POLICY resolver_revoke ON sessions;
REVOKE UPDATE (revoked_at) ON sessions FROM app_resolver;
REVOKE SELECT (verified_email) ON oauth_identities FROM app_resolver;
DROP POLICY resolver_status ON users;
REVOKE SELECT (created_at, primary_email, disabled_at), UPDATE (status, disabled_at, updated_at) ON users FROM app_resolver;
UPDATE users SET status = 'active' WHERE status = 'disabled';
ALTER TABLE users DROP CONSTRAINT users_disabled_at;
ALTER TABLE users DROP COLUMN disabled_at;
