-- 0016 retention sweeps (backlog T17; 05 "Retention and migrations", 08 "Lifecycle and controls").

-- Tenant data is swept per workspace by a worker in that workspace's context (app_runtime), like any
-- stage. Cross-tenant identity and audit rows are swept by SECURITY DEFINER functions whose deletes are
-- bounded by row-level security: each DELETE policy admits only rows past their retention window, so not
-- even a defective caller of these functions can remove anything younger.

-- Expired export packages keep their row (downloads stay 410) but lose the object.
ALTER TABLE exports DROP CONSTRAINT exports_status_check;
ALTER TABLE exports ADD CONSTRAINT exports_status_check CHECK (status IN ('pending', 'ready', 'failed', 'expired'));

-- Builds and snapshots whose content retention removed (the rows stay for the ledger and history).
ALTER TABLE builds ADD COLUMN content_purged_at timestamptz;
ALTER TABLE snapshots ADD COLUMN content_purged_at timestamptz;
CREATE INDEX ix_builds_retention ON builds(workspace_id, completed_at) WHERE content_purged_at IS NULL;

-- The sweeper pages through workspaces by ID (identifiers only).
CREATE FUNCTION app_private.workspaces_after(p_after uuid, p_limit integer) RETURNS SETOF uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT w.id FROM public.workspaces w
   WHERE w.status IN ('active', 'suspended') AND (p_after IS NULL OR w.id > p_after)
   ORDER BY w.id LIMIT LEAST(p_limit, 500)
$$;

-- Identity rows: expired idempotency keys (24 h), sign-in transactions a day after expiry, sessions 30 days
-- after they ended. Transactions go first: they reference sessions.
GRANT SELECT (expires_at), DELETE ON idempotency_keys TO app_resolver;
CREATE POLICY resolver_retention ON idempotency_keys FOR DELETE TO app_resolver USING (expires_at < now());
-- A DELETE with a WHERE clause also needs the rows to pass a SELECT policy: bounded the same way.
CREATE POLICY resolver_retention_read ON idempotency_keys FOR SELECT TO app_resolver USING (expires_at < now());
GRANT DELETE ON oauth_transactions TO app_resolver;
GRANT SELECT (expires_at, revoked_at), DELETE ON sessions TO app_resolver;
CREATE POLICY resolver_retention ON sessions FOR DELETE TO app_resolver
  USING (COALESCE(revoked_at, expires_at) < now() - interval '30 days');
CREATE FUNCTION app_private.sweep_identity()
  RETURNS TABLE (idempotency_keys bigint, oauth_transactions bigint, sessions bigint)
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  k bigint; t bigint; s bigint;
BEGIN
  DELETE FROM public.idempotency_keys WHERE expires_at < now();
  GET DIAGNOSTICS k = ROW_COUNT;
  DELETE FROM public.oauth_transactions
   WHERE expires_at < now() - interval '1 day'
      OR initiating_session_id IN (SELECT x.id FROM public.sessions x
                                    WHERE COALESCE(x.revoked_at, x.expires_at) < now() - interval '30 days');
  GET DIAGNOSTICS t = ROW_COUNT;
  DELETE FROM public.sessions WHERE COALESCE(revoked_at, expires_at) < now() - interval '30 days';
  GET DIAGNOSTICS s = ROW_COUNT;
  RETURN QUERY SELECT k, t, s;
END $$;

-- Audit events are append-only for every role; only events older than 180 days can be removed, and only
-- through this function (05: controlled purge, not a runtime privilege).
GRANT SELECT (created_at), DELETE ON audit_events TO app_resolver;
CREATE POLICY resolver_retention ON audit_events FOR DELETE TO app_resolver
  USING (created_at < now() - interval '180 days');
CREATE POLICY resolver_retention_read ON audit_events FOR SELECT TO app_resolver
  USING (created_at < now() - interval '180 days');
CREATE FUNCTION app_private.sweep_audit() RETURNS bigint
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  n bigint;
BEGIN
  DELETE FROM public.audit_events WHERE created_at < now() - interval '180 days';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

ALTER FUNCTION app_private.workspaces_after(uuid, integer) OWNER TO app_resolver;
ALTER FUNCTION app_private.sweep_identity() OWNER TO app_resolver;
ALTER FUNCTION app_private.sweep_audit() OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.workspaces_after(uuid, integer), app_private.sweep_identity(),
  app_private.sweep_audit() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.workspaces_after(uuid, integer), app_private.sweep_identity(),
  app_private.sweep_audit() TO app_runtime;

-- Dispatched outbox rows are kept 7 days (the scheduler removes them).
GRANT SELECT (created_at), DELETE ON outbox TO app_system;

-- Security review of 0015: an operator takedown must survive project deletion, or a new project could
-- claim the same public address. The purge keeps operator-suspended publication rows (identifiers only;
-- the address stays reserved), and the 0012 trigger again refuses deleting them in every case.
CREATE OR REPLACE FUNCTION app_private.keep_operator_suspension() RETURNS trigger
  LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF OLD.suspended_reason = 'operator' THEN
    RAISE EXCEPTION 'publication suspended by an operator' USING ERRCODE = '23514',
      CONSTRAINT = 'publications_keep_operator_suspension';
  END IF;
  RETURN OLD;
END $$;
