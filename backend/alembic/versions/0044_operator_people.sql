-- 0044 operator controls (02 S21, S16; 09 "operations role", "spending limits"; 11 credits and member limits).
--
-- 1. Operators list people and owners with their activity and spending: handles, IDs, states, counts and
--    money, never names or emails. A person is found by handle, or by typing their exact email (matched,
--    never shown).
-- 2. Operators suspend a person: no sign-in, every session, access token and MCP sign-in ends, and their
--    queued or running builds are cancelled (the caller cancels the builds this function returns).
--    Workspaces and sites stay up. Reinstating lets the person sign in again; ended tokens stay ended.
-- 3. Operators credit an owner's spending this month: a grant (kind `credit`) of what the owner has
--    settled so far, ending with the month. The ledger is never edited (22).
-- 4. Workspace owners and admins set a monthly spending limit per member (`member_limits`), checked at
--    build admission against the allowance-funded builds that member started this month.

-- ---------------------------------------------------------------- people
ALTER TABLE users ADD COLUMN disabled_at timestamptz;
ALTER TABLE users ADD CONSTRAINT users_disabled_at CHECK (status <> 'disabled' OR disabled_at IS NOT NULL);
GRANT SELECT (created_at, primary_email, disabled_at), UPDATE (status, disabled_at, updated_at) ON users TO app_resolver;
CREATE POLICY resolver_status ON users FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT (verified_email) ON oauth_identities TO app_resolver;
GRANT UPDATE (revoked_at) ON sessions TO app_resolver;
CREATE POLICY resolver_revoke ON sessions FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT (user_id) ON mcp_sessions TO app_resolver;
GRANT SELECT (created_by, usage_account_id) ON builds TO app_resolver;
GRANT SELECT (status, plan_key) ON subscriptions TO app_resolver;

-- Build admission refuses a suspended person (pushes act for their policy's admin, so they stop too).
CREATE FUNCTION app_private.user_active(p_user uuid) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT EXISTS (SELECT 1 FROM public.users u WHERE u.id = p_user AND u.status = 'active')
$$;

-- Sign-in tells a suspended person why (identity_user answers only for active people).
CREATE FUNCTION app_private.identity_suspended(p_provider text, p_issuer text, p_subject text) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT EXISTS (SELECT 1 FROM public.oauth_identities i JOIN public.users u ON u.id = i.user_id
                  WHERE i.provider = p_provider AND i.issuer = p_issuer AND i.subject = p_subject
                    AND u.status = 'disabled')
$$;

-- This month's allowance-funded spending of builds a person started (settled and still held).
CREATE FUNCTION app_private.person_month_spend(p_user uuid)
  RETURNS TABLE (builds bigint, spent_micro_usd bigint)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*), COALESCE(sum(b.settled_micro_usd + b.reserved_micro_usd), 0)::bigint
    FROM public.builds b JOIN public.usage_accounts a ON a.workspace_id = b.workspace_id AND a.id = b.usage_account_id
   WHERE b.created_by = p_user AND a.period_start = date_trunc('month', now(), 'UTC')
$$;

CREATE FUNCTION app_private.ops_people(
  p_handle text, p_email text, p_status text, p_after_created timestamptz, p_after_id uuid, p_limit integer)
  RETURNS TABLE (user_id uuid, handle text, status text, created_at timestamptz, disabled_at timestamptz,
                 last_active_at timestamptz, workspaces bigint, organizations bigint, builds_this_month bigint,
                 spent_this_month_micro_usd bigint, operator boolean)
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  email text := NULLIF(lower(btrim(COALESCE(p_email, ''))), '');
  prefix text := NULLIF(lower(btrim(COALESCE(p_handle, ''))), '');
BEGIN
  PERFORM app_private.require_operator();
  RETURN QUERY
  SELECT u.id, n.handle, u.status, u.created_at, u.disabled_at,
         (SELECT max(s.last_seen_at) FROM public.sessions s WHERE s.user_id = u.id),
         (SELECT count(*) FROM public.memberships m JOIN public.workspaces w ON w.id = m.workspace_id
           WHERE m.user_id = u.id AND w.status = 'active'),
         (SELECT count(*) FROM public.organization_members om JOIN public.organizations o ON o.id = om.organization_id
           WHERE om.user_id = u.id AND o.status = 'active'),
         spend.builds, spend.spent_micro_usd,
         EXISTS (SELECT 1 FROM public.operators o WHERE o.user_id = u.id AND o.revoked_at IS NULL)
    FROM public.users u
    LEFT JOIN public.namespaces n ON n.user_id = u.id
    CROSS JOIN LATERAL app_private.person_month_spend(u.id) spend
   WHERE u.status <> 'deleted'
     AND (p_status IS NULL OR u.status = p_status)
     AND (prefix IS NULL OR starts_with(n.handle, prefix))
     AND (email IS NULL OR lower(u.primary_email) = email OR EXISTS (
            SELECT 1 FROM public.oauth_identities i WHERE i.user_id = u.id AND lower(i.verified_email) = email))
     AND (p_after_created IS NULL OR (u.created_at, u.id) < (p_after_created, p_after_id))
   ORDER BY u.created_at DESC, u.id DESC
   LIMIT LEAST(GREATEST(p_limit, 1), 100);
END $$;

-- One person: where they belong and what they spent there this month (IDs, handles and roles only).
CREATE FUNCTION app_private.ops_person(p_user uuid) RETURNS jsonb
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM app_private.require_operator();
  RETURN (
    SELECT jsonb_build_object(
      'user_id', u.id, 'handle', (SELECT n.handle FROM public.namespaces n WHERE n.user_id = u.id),
      'status', u.status, 'created_at', u.created_at, 'disabled_at', u.disabled_at,
      'last_active_at', (SELECT max(s.last_seen_at) FROM public.sessions s WHERE s.user_id = u.id),
      'operator', EXISTS (SELECT 1 FROM public.operators o WHERE o.user_id = u.id AND o.revoked_at IS NULL),
      'workspaces', COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'workspace_id', w.id, 'owner_handle', wn.handle, 'owner_kind', wn.kind, 'role', m.role,
          'via_organization', m.via_organization,
          'builds_this_month', (SELECT count(*) FROM public.builds b JOIN public.usage_accounts a
              ON a.workspace_id = b.workspace_id AND a.id = b.usage_account_id
             WHERE b.workspace_id = w.id AND b.created_by = u.id AND a.period_start = date_trunc('month', now(), 'UTC')),
          'spent_this_month_micro_usd', (SELECT COALESCE(sum(b.settled_micro_usd + b.reserved_micro_usd), 0)
              FROM public.builds b JOIN public.usage_accounts a ON a.workspace_id = b.workspace_id AND a.id = b.usage_account_id
             WHERE b.workspace_id = w.id AND b.created_by = u.id AND a.period_start = date_trunc('month', now(), 'UTC')))
          ORDER BY wn.handle, w.id)
        FROM public.memberships m JOIN public.workspaces w ON w.id = m.workspace_id AND w.status = 'active'
        LEFT JOIN public.namespaces wn ON wn.id = w.namespace_id
       WHERE m.user_id = u.id), '[]'::jsonb),
      'organizations', COALESCE((SELECT jsonb_agg(jsonb_build_object(
          'organization_id', o.id, 'handle', onm.handle, 'role', om.role) ORDER BY onm.handle)
        FROM public.organization_members om JOIN public.organizations o ON o.id = om.organization_id AND o.status = 'active'
        LEFT JOIN public.namespaces onm ON onm.organization_id = o.id
       WHERE om.user_id = u.id), '[]'::jsonb),
      'sign_in', COALESCE((SELECT jsonb_agg(DISTINCT i.provider) FROM public.oauth_identities i WHERE i.user_id = u.id), '[]'::jsonb),
      'live_sessions', (SELECT count(*) FROM public.sessions s
                         WHERE s.user_id = u.id AND s.revoked_at IS NULL AND s.expires_at > now()),
      'live_tokens', (SELECT count(*) FROM public.access_tokens t
                       WHERE t.user_id = u.id AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > now())))
    FROM public.users u WHERE u.id = p_user AND u.status <> 'deleted');
END $$;

-- Suspend or reinstate a person. Returns the queued or running builds they started, for the caller to
-- cancel (cancellation writes build events and may stop tasks, which the application does). Refuses the
-- caller themselves and other operators (revoke the role first, `python -m app.ops.operators`).
CREATE FUNCTION app_private.ops_set_person_status(p_user uuid, p_suspend boolean, p_reason text)
  RETURNS TABLE (workspace_id uuid, build_id uuid)
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  op uuid := app_private.require_operator();
  current_status text;
  changed boolean := false;
  why text := left(COALESCE(p_reason, ''), 500);
BEGIN
  SELECT u.status INTO current_status FROM public.users u WHERE u.id = p_user FOR UPDATE;
  IF current_status IS NULL OR current_status = 'deleted' THEN
    RAISE EXCEPTION 'person not found' USING ERRCODE = 'P0002';
  END IF;
  IF p_user = op OR EXISTS (SELECT 1 FROM public.operators o WHERE o.user_id = p_user AND o.revoked_at IS NULL) THEN
    RAISE EXCEPTION 'operators cannot be suspended here' USING ERRCODE = '55000';
  END IF;
  IF p_suspend THEN
    IF current_status = 'active' THEN
      UPDATE public.users SET status = 'disabled', disabled_at = now(), updated_at = now() WHERE id = p_user;
      changed := true;
    END IF;
    -- Every way in ends, also on a repeated call (a token issued in a race before the status changed).
    UPDATE public.sessions SET revoked_at = now() WHERE user_id = p_user AND revoked_at IS NULL;
    UPDATE public.access_tokens SET revoked_at = now() WHERE user_id = p_user AND revoked_at IS NULL;
    UPDATE public.oauth_grants SET revoked_at = now() WHERE user_id = p_user AND revoked_at IS NULL;
    DELETE FROM public.mcp_sessions s WHERE s.user_id = p_user;
  ELSIF current_status = 'disabled' THEN
    UPDATE public.users SET status = 'active', disabled_at = NULL, updated_at = now() WHERE id = p_user;
    changed := true;
  END IF;
  IF changed THEN
    INSERT INTO public.operator_events(operator_user_id, action, workspace_id, target_id, reason)
    VALUES (op, CASE WHEN p_suspend THEN 'person.suspended' ELSE 'person.reinstated' END, NULL, p_user, why);
    -- Each workspace the person belongs to sees it in its audit log.
    INSERT INTO public.audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata)
    SELECT m.workspace_id, op, CASE WHEN p_suspend THEN 'member.suspended' ELSE 'member.reinstated' END,
           'membership', m.id, jsonb_build_object('operator', true, 'user_id', p_user, 'reason', why)
      FROM public.memberships m JOIN public.workspaces w ON w.id = m.workspace_id AND w.status = 'active'
     WHERE m.user_id = p_user;
  END IF;
  IF p_suspend THEN
    RETURN QUERY SELECT b.workspace_id, b.id FROM public.builds b
      WHERE b.created_by = p_user AND b.status IN ('queued', 'running') AND b.cancel_requested_at IS NULL
      ORDER BY b.workspace_id, b.id;
  END IF;
END $$;

-- Owners (people and organizations) with this month's spending. The allowance itself depends on the
-- plan catalog and the deployment default, which the application adds (`held` names the subscription
-- states that hold a plan).
CREATE FUNCTION app_private.ops_owners(p_handle text, p_after text, p_limit integer, p_held text[])
  RETURNS TABLE (namespace_id uuid, handle text, kind text, status text, workspaces bigint, projects bigint,
                 published_sites bigint, builds_this_month bigint, settled_micro_usd bigint,
                 reserved_micro_usd bigint, granted_micro_usd bigint, plan_key text)
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  prefix text := NULLIF(lower(btrim(COALESCE(p_handle, ''))), '');
  month_start timestamptz := date_trunc('month', now(), 'UTC');
BEGIN
  PERFORM app_private.require_operator();
  RETURN QUERY
  SELECT n.id, n.handle, n.kind,
         COALESCE(u.status, o.status),
         (SELECT count(*) FROM public.workspaces w WHERE w.namespace_id = n.id AND w.status = 'active'),
         (SELECT count(*) FROM public.projects p JOIN public.workspaces w ON w.id = p.workspace_id AND w.status = 'active'
           WHERE w.namespace_id = n.id AND p.deleted_at IS NULL),
         (SELECT count(*) FROM public.publications pu JOIN public.workspaces w ON w.id = pu.workspace_id AND w.status = 'active'
           JOIN public.projects p ON p.workspace_id = pu.workspace_id AND p.id = pu.project_id AND p.deleted_at IS NULL
           WHERE w.namespace_id = n.id AND pu.build_id IS NOT NULL AND pu.suspended_at IS NULL),
         (SELECT count(*) FROM public.builds b JOIN public.usage_accounts a ON a.workspace_id = b.workspace_id AND a.id = b.usage_account_id
           JOIN public.workspaces w ON w.id = b.workspace_id WHERE w.namespace_id = n.id AND a.period_start = month_start),
         (SELECT COALESCE(sum(a.settled_micro_usd), 0)::bigint FROM public.usage_accounts a
           JOIN public.workspaces w ON w.id = a.workspace_id WHERE w.namespace_id = n.id AND a.period_start = month_start),
         (SELECT COALESCE(sum(a.reserved_micro_usd), 0)::bigint FROM public.usage_accounts a
           JOIN public.workspaces w ON w.id = a.workspace_id WHERE w.namespace_id = n.id AND a.period_start = month_start),
         (SELECT COALESCE(sum(g.allowance_micro_usd), 0)::bigint FROM public.entitlement_grants g
           WHERE g.namespace_id = n.id AND g.revoked_at IS NULL AND g.starts_at < month_start + interval '1 month'
             AND g.expires_at > month_start AND (NOT g.once OR g.starts_at >= month_start)),
         (SELECT s.plan_key FROM public.subscriptions s WHERE s.namespace_id = n.id AND s.status = ANY(p_held)
           ORDER BY s.updated_at DESC LIMIT 1)
    FROM public.namespaces n
    LEFT JOIN public.users u ON u.id = n.user_id
    LEFT JOIN public.organizations o ON o.id = n.organization_id
   WHERE (u.id IS NOT NULL AND u.status <> 'deleted' OR o.id IS NOT NULL AND o.status = 'active')
     AND (prefix IS NULL OR starts_with(n.handle, prefix))
     AND (p_after IS NULL OR n.handle > p_after)
   ORDER BY n.handle
   LIMIT LEAST(GREATEST(p_limit, 1), 100);
END $$;

-- Credit what an owner has settled this month and no earlier credit covers: a grant of that amount,
-- ending with the month, through the owner's oldest active workspace. "Reset usage" without editing the
-- ledger (22).
ALTER TABLE entitlement_grants DROP CONSTRAINT entitlement_grants_kind_check;
ALTER TABLE entitlement_grants ADD CONSTRAINT entitlement_grants_kind_check CHECK (kind IN ('manual', 'trial', 'credit'));

CREATE FUNCTION app_private.ops_credit_owner(p_namespace uuid, p_reason text) RETURNS SETOF entitlement_grants
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  op uuid := app_private.require_operator();
  month_start timestamptz := date_trunc('month', now(), 'UTC');
  ws uuid;
  amount bigint;
  g uuid;
BEGIN
  SELECT w.id INTO ws FROM public.workspaces w WHERE w.namespace_id = p_namespace AND w.status = 'active'
   ORDER BY w.id LIMIT 1;
  IF ws IS NULL THEN
    RAISE EXCEPTION 'owner has no active workspace' USING ERRCODE = 'P0002';
  END IF;
  -- Serialize with build admission and other credits of this owner (ledger.lock_owner).
  PERFORM pg_advisory_xact_lock(hashtextextended('billing-account:' || p_namespace::text, 0));
  -- What was settled this month, less what earlier credits this month already handed back.
  SELECT COALESCE(sum(a.settled_micro_usd), 0) INTO amount FROM public.usage_accounts a
    JOIN public.workspaces w ON w.id = a.workspace_id
   WHERE w.namespace_id = p_namespace AND a.period_start = month_start;
  amount := amount - (SELECT COALESCE(sum(g.allowance_micro_usd), 0) FROM public.entitlement_grants g
                       WHERE g.namespace_id = p_namespace AND g.kind = 'credit' AND g.revoked_at IS NULL
                         AND g.starts_at >= month_start);
  IF amount <= 0 THEN
    RAISE EXCEPTION 'nothing to credit this month' USING ERRCODE = '55000';
  END IF;
  INSERT INTO public.entitlement_grants(workspace_id, allowance_micro_usd, starts_at, expires_at, reason, granted_by, kind)
  VALUES (ws, LEAST(amount, 1000000000), now(), month_start + interval '1 month', p_reason, op, 'credit')
  RETURNING id INTO g;
  PERFORM app_private.ops_event('entitlement.credited', ws, g, p_reason);
  RETURN QUERY SELECT * FROM public.entitlement_grants e WHERE e.id = g;
END $$;

DO $$
DECLARE
  f text;
BEGIN
  FOREACH f IN ARRAY ARRAY[
    'user_active(uuid)', 'identity_suspended(text, text, text)', 'person_month_spend(uuid)',
    'ops_people(text, text, text, timestamptz, uuid, integer)', 'ops_person(uuid)',
    'ops_set_person_status(uuid, boolean, text)', 'ops_owners(text, text, integer, text[])',
    'ops_credit_owner(uuid, text)'
  ] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;
-- Internal to the operator functions.
REVOKE EXECUTE ON FUNCTION app_private.person_month_spend(uuid) FROM app_runtime;

-- ---------------------------------------------------------------- member spending limits
-- A member's monthly limit in one workspace. Ends with the membership.
CREATE TABLE member_limits (
  workspace_id uuid NOT NULL,
  user_id uuid NOT NULL,
  monthly_limit_micro_usd bigint NOT NULL CHECK (monthly_limit_micro_usd >= 0 AND monthly_limit_micro_usd <= 1000000000),
  set_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id),
  FOREIGN KEY (workspace_id, user_id) REFERENCES memberships(workspace_id, user_id) ON DELETE CASCADE
);
ALTER TABLE member_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE member_limits FORCE ROW LEVEL SECURITY;
REVOKE ALL ON member_limits FROM app_runtime;
CREATE POLICY tenant ON member_limits TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, DELETE ON member_limits TO app_runtime;
GRANT UPDATE (monthly_limit_micro_usd, set_by, updated_at) ON member_limits TO app_runtime;
