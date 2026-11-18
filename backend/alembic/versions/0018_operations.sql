-- 0018 platform operations (M4; 02 S21, 08 "support access", 09 "operations role", 11 manual entitlements,
-- 22 kill switch).

-- Operators are a separate role, not tenant membership (09). They are granted outside the product, by the
-- deployment (`python -m app.ops.operators grant`, DDL role); no application role can write this table.
-- Every operator action runs through a SECURITY DEFINER function below that first checks the caller
-- (`app.user_id`, set from the authenticated session) against this table, records an operator event and,
-- when a workspace is affected, a tenant audit event the customer sees.
CREATE TABLE operators (
  user_id uuid PRIMARY KEY REFERENCES users(id),
  granted_at timestamptz NOT NULL DEFAULT now(),
  note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 200),
  revoked_at timestamptz
);
GRANT SELECT ON operators TO app_resolver;

CREATE TABLE operator_events (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  operator_user_id uuid NOT NULL REFERENCES users(id),
  action text NOT NULL CHECK (action ~ '^[a-z][a-z_.]{0,63}$'),
  workspace_id uuid,
  target_id uuid,
  reason text NOT NULL DEFAULT '' CHECK (char_length(reason) <= 500),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_operator_events_created ON operator_events(created_at);
GRANT SELECT, INSERT ON operator_events TO app_resolver;
-- Not tenant data, but it names workspaces: FORCE RLS like every such table. The operator functions
-- (resolver) and the deployment's DDL role (the operator CLI) are its only readers and writers.
CREATE POLICY resolver ON operator_events TO app_resolver USING (true) WITH CHECK (true);
CREATE POLICY deployment ON operator_events TO CURRENT_USER USING (true) WITH CHECK (true);
ALTER TABLE operator_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE operator_events FORCE ROW LEVEL SECURITY;

-- The platform's switches: one row. Reads are open to the runtime (build admission checks them).
CREATE TABLE platform_settings (
  id boolean PRIMARY KEY DEFAULT true CHECK (id),
  builds_paused boolean NOT NULL DEFAULT false,
  paused_reason text NOT NULL DEFAULT '' CHECK (char_length(paused_reason) <= 500),
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO platform_settings DEFAULT VALUES;
GRANT SELECT ON platform_settings TO app_runtime;
GRANT SELECT, UPDATE (builds_paused, paused_reason, updated_at) ON platform_settings TO app_resolver;

-- Manual entitlements for the closed beta (11): extra allowance for the usage periods a grant overlaps.
CREATE TABLE entitlement_grants (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  allowance_micro_usd bigint NOT NULL CHECK (allowance_micro_usd > 0 AND allowance_micro_usd <= 1000000000),
  starts_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  reason text NOT NULL CHECK (char_length(btrim(reason)) >= 3 AND char_length(reason) <= 500),
  granted_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  CHECK (expires_at > starts_at AND expires_at <= starts_at + interval '366 days'),
  UNIQUE (workspace_id, id)
);
CREATE INDEX ix_entitlement_grants_workspace ON entitlement_grants(workspace_id, expires_at);
GRANT SELECT ON entitlement_grants TO app_runtime;
CREATE POLICY tenant ON entitlement_grants FOR SELECT TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY resolver ON entitlement_grants TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE (revoked_at) ON entitlement_grants TO app_resolver;
ALTER TABLE entitlement_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE entitlement_grants FORCE ROW LEVEL SECURITY;

-- Support access (08): a workspace admin lets platform operators read the workspace, read-only, for a
-- bounded time with a reason, and can revoke it. Without a live grant an operator sees nothing of it.
CREATE TABLE support_grants (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  granted_by uuid NOT NULL REFERENCES users(id),
  reason text NOT NULL CHECK (char_length(btrim(reason)) >= 3 AND char_length(reason) <= 500),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  CHECK (expires_at > created_at AND expires_at <= created_at + interval '72 hours'),
  UNIQUE (workspace_id, id)
);
CREATE INDEX ix_support_grants_workspace ON support_grants(workspace_id, expires_at);
GRANT SELECT, INSERT, UPDATE (revoked_at) ON support_grants TO app_runtime;
CREATE POLICY tenant ON support_grants TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY resolver ON support_grants FOR SELECT TO app_resolver USING (true);
GRANT SELECT (workspace_id, expires_at, revoked_at) ON support_grants TO app_resolver;
ALTER TABLE support_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_grants FORCE ROW LEVEL SECURITY;

-- Tenant audit events written by operator functions, and the columns they read.
CREATE POLICY resolver_operator_insert ON audit_events FOR INSERT TO app_resolver WITH CHECK (true);
GRANT INSERT ON audit_events TO app_resolver;
GRANT SELECT (suspended_reason) ON publications TO app_resolver;
GRANT SELECT (status, created_at, completed_at, cancel_requested_at, engine_version, prompt_version,
              cap_micro_usd, reserved_micro_usd, settled_micro_usd) ON builds TO app_resolver;
GRANT SELECT (id, build_id, stage, attempt_count, lease_generation, error_code, updated_at, snapshot_id)
  ON build_tasks TO app_resolver;
GRANT SELECT (build_id, purpose, settled_micro_usd, error_code) ON provider_attempts TO app_resolver;
GRANT SELECT (build_id) ON build_events TO app_resolver;
CREATE POLICY resolver_ops ON build_events FOR SELECT TO app_resolver USING (true);

-- Local bootstrap grants the runtime everything on new tables by default (deployment/init-db.sh). These
-- tables decide who is an operator, what the platform allows and what a workspace may spend, so the
-- runtime gets exactly the reads it needs and nothing else, wherever the database came from.
REVOKE ALL ON operators, operator_events, platform_settings FROM app_runtime;
GRANT SELECT ON platform_settings TO app_runtime;
REVOKE INSERT, UPDATE, DELETE ON entitlement_grants FROM app_runtime;
REVOKE DELETE ON support_grants FROM app_runtime;
-- Tombstones (0015) are never deleted by the application: restores depend on them.
REVOKE DELETE ON deletion_requests FROM app_runtime;

-- ---------------------------------------------------------------- functions
-- The operator functions call each other (the role check, the event helper), as their owner.
GRANT USAGE ON SCHEMA app_private TO app_resolver;

CREATE FUNCTION app_private.is_operator() RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT EXISTS (SELECT 1 FROM public.operators o
                  WHERE o.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                    AND o.revoked_at IS NULL)
$$;

CREATE FUNCTION app_private.require_operator() RETURNS uuid
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF NOT app_private.is_operator() THEN
    RAISE EXCEPTION 'operator role required' USING ERRCODE = '42501';
  END IF;
  RETURN NULLIF(current_setting('app.user_id', true), '')::uuid;
END $$;

-- Does the calling operator hold live support access to this workspace?
CREATE FUNCTION app_private.support_access(p_workspace uuid) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT app_private.is_operator() AND EXISTS (
    SELECT 1 FROM public.support_grants g
     WHERE g.workspace_id = p_workspace AND g.revoked_at IS NULL AND g.expires_at > now())
$$;

CREATE FUNCTION app_private.ops_event(p_action text, p_workspace uuid, p_target uuid, p_reason text) RETURNS void
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  op uuid := app_private.require_operator();
BEGIN
  INSERT INTO public.operator_events(operator_user_id, action, workspace_id, target_id, reason)
  VALUES (op, p_action, p_workspace, p_target, left(COALESCE(p_reason, ''), 500));
  IF p_workspace IS NOT NULL THEN
    INSERT INTO public.audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata)
    VALUES (p_workspace, op, p_action, 'operator', p_target,
            jsonb_build_object('operator', true, 'reason', left(COALESCE(p_reason, ''), 500)));
  END IF;
END $$;

CREATE FUNCTION app_private.ops_summary() RETURNS jsonb
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM app_private.require_operator();
  RETURN jsonb_build_object(
    'builds_last_24h', COALESCE((SELECT jsonb_object_agg(s.status, s.n) FROM
        (SELECT b.status, count(*) AS n FROM public.builds b WHERE b.created_at > now() - interval '24 hours'
          GROUP BY b.status) s), '{}'::jsonb),
    'tasks_by_stage', COALESCE((SELECT jsonb_object_agg(s.key, s.n) FROM
        (SELECT t.stage || ':' || t.state AS key, count(*) AS n FROM public.build_tasks t
          WHERE t.state IN ('pending', 'leased', 'retry_wait') GROUP BY t.stage, t.state) s), '{}'::jsonb),
    'platform', (SELECT jsonb_build_object('builds_paused', p.builds_paused, 'paused_reason', p.paused_reason,
                                           'updated_at', p.updated_at) FROM public.platform_settings p));
END $$;

CREATE FUNCTION app_private.ops_build(p_build uuid) RETURNS jsonb
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM app_private.require_operator();
  RETURN (
    SELECT jsonb_build_object(
      'id', b.id, 'workspace_id', b.workspace_id, 'project_id', b.project_id, 'status', b.status,
      'created_at', b.created_at, 'completed_at', b.completed_at, 'cancel_requested_at', b.cancel_requested_at,
      'engine_version', b.engine_version, 'prompt_version', b.prompt_version,
      'cap_micro_usd', b.cap_micro_usd, 'reserved_micro_usd', b.reserved_micro_usd,
      'settled_micro_usd', b.settled_micro_usd,
      'tasks', COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'id', t.id, 'stage', t.stage, 'state', t.state, 'attempts', t.attempt_count,
          'lease_generation', t.lease_generation, 'lease_until', t.lease_until,
          'next_attempt_at', t.next_attempt_at, 'error_code', t.error_code,
          'created_at', t.created_at, 'updated_at', t.updated_at) ORDER BY t.created_at, t.id)
        FROM public.build_tasks t WHERE t.build_id = b.id), '[]'::jsonb),
      'provider_attempts', COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'purpose', a.purpose, 'state', a.state, 'count', a.n, 'settled_micro_usd', a.settled,
          'error_codes', a.codes) ORDER BY a.purpose, a.state)
        FROM (SELECT x.purpose, x.state, count(*) AS n, COALESCE(sum(x.settled_micro_usd), 0) AS settled,
                     COALESCE(jsonb_agg(DISTINCT x.error_code) FILTER (WHERE x.error_code IS NOT NULL), '[]') AS codes
                FROM public.provider_attempts x WHERE x.build_id = b.id GROUP BY x.purpose, x.state) a), '[]'::jsonb),
      'events', (SELECT count(*) FROM public.build_events e WHERE e.build_id = b.id))
    FROM public.builds b WHERE b.id = p_build);
END $$;

CREATE FUNCTION app_private.ops_set_builds_paused(p_paused boolean, p_reason text) RETURNS void
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM app_private.require_operator();
  UPDATE public.platform_settings SET builds_paused = p_paused,
    paused_reason = CASE WHEN p_paused THEN left(COALESCE(p_reason, ''), 500) ELSE '' END, updated_at = now();
  PERFORM app_private.ops_event(CASE WHEN p_paused THEN 'builds.paused' ELSE 'builds.resumed' END,
                                NULL, NULL, p_reason);
END $$;

-- Operator takedown of a project's publication (05 publications.suspended_reason = 'operator').
CREATE FUNCTION app_private.ops_suspend_publication(p_project uuid, p_suspend boolean, p_reason text)
  RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  w uuid;
BEGIN
  PERFORM app_private.require_operator();
  IF p_suspend THEN
    UPDATE public.publications SET suspended_at = now(), suspended_reason = 'operator', updated_at = now()
     WHERE project_id = p_project RETURNING workspace_id INTO w;
  ELSE
    UPDATE public.publications SET suspended_at = NULL, suspended_reason = NULL, updated_at = now()
     WHERE project_id = p_project AND suspended_reason = 'operator' RETURNING workspace_id INTO w;
  END IF;
  IF w IS NOT NULL THEN
    PERFORM app_private.ops_event(CASE WHEN p_suspend THEN 'publication.suspended' ELSE 'publication.reinstated' END,
                                  w, p_project, p_reason);
  END IF;
  RETURN w;
END $$;

CREATE FUNCTION app_private.ops_grant_entitlement(p_workspace uuid, p_amount bigint, p_days integer, p_reason text)
  RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  op uuid := app_private.require_operator();
  g uuid;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = p_workspace AND w.status = 'active') THEN
    RETURN NULL;
  END IF;
  INSERT INTO public.entitlement_grants(workspace_id, allowance_micro_usd, expires_at, reason, granted_by)
  VALUES (p_workspace, p_amount, now() + make_interval(days => p_days), p_reason, op) RETURNING id INTO g;
  PERFORM app_private.ops_event('entitlement.granted', p_workspace, g, p_reason);
  RETURN g;
END $$;

CREATE FUNCTION app_private.ops_revoke_entitlement(p_grant uuid, p_reason text) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  w uuid;
BEGIN
  PERFORM app_private.require_operator();
  UPDATE public.entitlement_grants SET revoked_at = now()
   WHERE id = p_grant AND revoked_at IS NULL RETURNING workspace_id INTO w;
  IF w IS NOT NULL THEN
    PERFORM app_private.ops_event('entitlement.revoked', w, p_grant, p_reason);
  END IF;
  RETURN w;
END $$;

CREATE FUNCTION app_private.ops_entitlements(p_workspace uuid) RETURNS SETOF entitlement_grants
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM app_private.require_operator();
  RETURN QUERY SELECT * FROM public.entitlement_grants g WHERE g.workspace_id = p_workspace ORDER BY g.created_at DESC;
END $$;

CREATE FUNCTION app_private.ops_events(p_before timestamptz, p_limit integer) RETURNS SETOF operator_events
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM app_private.require_operator();
  RETURN QUERY SELECT * FROM public.operator_events e
    WHERE p_before IS NULL OR e.created_at < p_before ORDER BY e.created_at DESC LIMIT LEAST(p_limit, 200);
END $$;

-- The job-system gauges (0017) for the operations view, behind the operator check.
CREATE FUNCTION app_private.ops_gauges_for_operator()
  RETURNS TABLE (
    runnable_tasks bigint, oldest_runnable_seconds double precision, leased_tasks bigint,
    expired_leases bigint, outbox_unpublished bigint, outbox_oldest_seconds double precision,
    deletions_pending bigint, deletion_oldest_seconds double precision, unknown_attempts bigint,
    unknown_oldest_seconds double precision, usage_drift_micro_usd numeric)
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM app_private.require_operator();
  RETURN QUERY SELECT * FROM app_private.ops_gauges();
END $$;

DO $$
DECLARE
  f text;
BEGIN
  FOREACH f IN ARRAY ARRAY[
    'is_operator()', 'require_operator()', 'support_access(uuid)', 'ops_event(text, uuid, uuid, text)',
    'ops_summary()', 'ops_build(uuid)', 'ops_set_builds_paused(boolean, text)',
    'ops_suspend_publication(uuid, boolean, text)', 'ops_grant_entitlement(uuid, bigint, integer, text)',
    'ops_revoke_entitlement(uuid, text)', 'ops_entitlements(uuid)', 'ops_events(timestamptz, integer)',
    'ops_gauges_for_operator()'
  ] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;
-- The event helper is internal: callable only from the operator functions above.
REVOKE EXECUTE ON FUNCTION app_private.ops_event(text, uuid, uuid, text) FROM app_runtime;
