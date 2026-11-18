CREATE OR REPLACE FUNCTION app_private.keep_operator_suspension() RETURNS trigger
  LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF OLD.suspended_reason = 'operator'
     AND NOT EXISTS (SELECT 1 FROM public.projects p WHERE p.id = OLD.project_id AND p.deleted_at IS NOT NULL) THEN
    RAISE EXCEPTION 'publication suspended by an operator' USING ERRCODE = '23514',
      CONSTRAINT = 'publications_keep_operator_suspension';
  END IF;
  RETURN OLD;
END $$;
REVOKE SELECT (created_at), DELETE ON outbox FROM app_system;
DROP FUNCTION app_private.sweep_audit();
DROP POLICY IF EXISTS resolver_retention_read ON audit_events;
DROP POLICY resolver_retention ON audit_events;
REVOKE SELECT (created_at), DELETE ON audit_events FROM app_resolver;
DROP FUNCTION app_private.sweep_identity();
DROP POLICY resolver_retention ON sessions;
REVOKE SELECT (expires_at, revoked_at), DELETE ON sessions FROM app_resolver;
REVOKE DELETE ON oauth_transactions FROM app_resolver;
DROP POLICY IF EXISTS resolver_retention_read ON idempotency_keys;
DROP POLICY resolver_retention ON idempotency_keys;
REVOKE SELECT (expires_at), DELETE ON idempotency_keys FROM app_resolver;
DROP FUNCTION app_private.workspaces_after(uuid, integer);
DROP INDEX ix_builds_retention;
ALTER TABLE snapshots DROP COLUMN content_purged_at;
ALTER TABLE builds DROP COLUMN content_purged_at;
UPDATE exports SET status = 'failed', error_code = COALESCE(error_code, 'expired') WHERE status = 'expired';
ALTER TABLE exports DROP CONSTRAINT exports_status_check;
ALTER TABLE exports ADD CONSTRAINT exports_status_check CHECK (status IN ('pending', 'ready', 'failed'));
