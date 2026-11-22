-- 0040 one plan per owner: billing moves from the workspace to its owner's namespace (11, 0038).
--
-- An organization (or a person, for their own workspaces) has one Stripe customer, one subscription,
-- one trial and one monthly allowance, shared by all of its workspaces; the plan's project, seat and
-- active-build limits count across them. Each workspace keeps its own ledger (usage_accounts and
-- usage_entries are unchanged): admission spends from the owner's allowance under one lock per owner,
-- giving the workspace what the other workspaces have not reserved or settled this month.
--
-- Billing rows keep the workspace they were created through (the anchor: the workspace a checkout was
-- started in, which Stripe's metadata names) and gain the owner's `namespace_id`, set from it on insert
-- and never changed. Every workspace of the owner reads them; writes still happen in the anchor's
-- context, as before.

-- ---------------------------------------------------------------- the owner on billing rows
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['billing_customers', 'subscriptions', 'entitlement_grants'] LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN namespace_id uuid REFERENCES namespaces(id)', t);
    EXECUTE format('UPDATE %I x SET namespace_id = w.namespace_id FROM workspaces w WHERE w.id = x.workspace_id', t);
  END LOOP;
END $$;

-- One customer per owner. A person who subscribed in two of their workspaces needs an operator first.
DO $$
DECLARE clash text;
BEGIN
  SELECT string_agg(namespace_id::text, ', ') INTO clash FROM (
    SELECT namespace_id FROM public.billing_customers WHERE namespace_id IS NOT NULL
     GROUP BY namespace_id HAVING count(*) > 1) x;
  IF clash IS NOT NULL THEN
    RAISE EXCEPTION 'several billing customers belong to one owner (namespaces %): cancel all but one subscription and remove the other customers before migrating', clash;
  END IF;
END $$;
CREATE UNIQUE INDEX uq_billing_customers_namespace ON billing_customers(namespace_id) WHERE namespace_id IS NOT NULL;
CREATE INDEX ix_subscriptions_namespace ON subscriptions(namespace_id, status);
CREATE INDEX ix_entitlement_grants_namespace ON entitlement_grants(namespace_id, expires_at);
-- The trial: once per owner (and, as before, once per person).
DROP INDEX uq_entitlement_trial;
CREATE UNIQUE INDEX uq_entitlement_trial ON entitlement_grants(namespace_id) WHERE kind = 'trial';

CREATE FUNCTION app_private.billing_namespace() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.namespace_id := (SELECT w.namespace_id FROM public.workspaces w WHERE w.id = NEW.workspace_id);
  ELSE
    NEW.namespace_id := OLD.namespace_id;  -- never moves to another owner
    NEW.workspace_id := OLD.workspace_id;  -- nor to another of its workspaces (security review)
  END IF;
  RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION app_private.billing_namespace() FROM PUBLIC;
ALTER FUNCTION app_private.billing_namespace() OWNER TO app_resolver;
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['billing_customers', 'subscriptions', 'entitlement_grants'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION app_private.billing_namespace()',
                   t || '_namespace', t);
  END LOOP;
END $$;
GRANT SELECT (id, namespace_id) ON workspaces TO app_resolver;

-- Every workspace of the owner reads the owner's billing rows; writes stay in the anchor workspace.
DROP POLICY tenant ON billing_customers;
DROP POLICY tenant ON subscriptions;
DROP POLICY tenant ON entitlement_grants;
-- Reads match the owner; inserts and updates only the row's own workspace (security review: a combined
-- policy would let another of the owner's workspaces update a row and claim it).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['billing_customers', 'subscriptions'] LOOP
    EXECUTE format('CREATE POLICY tenant ON %I FOR SELECT TO app_runtime
      USING (namespace_id = (SELECT w.namespace_id FROM public.workspaces w
                              WHERE w.id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid))', t);
    EXECUTE format('CREATE POLICY tenant_insert ON %I FOR INSERT TO app_runtime
      WITH CHECK (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
    EXECUTE format('CREATE POLICY tenant_update ON %I FOR UPDATE TO app_runtime
      USING (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)
      WITH CHECK (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
  END LOOP;
END $$;
CREATE POLICY tenant ON entitlement_grants FOR SELECT TO app_runtime
  USING (namespace_id = (SELECT w.namespace_id FROM public.workspaces w
                          WHERE w.id = NULLIF(current_setting('app.workspace_id', true), '')::uuid));

-- ---------------------------------------------------------------- the owner's totals (aggregates only)
-- For the workspace in context: what the owner's other workspaces have reserved and settled in a period,
-- and the owner's live projects, people (members and open invitations) and active builds, so limits count
-- across all of the owner's workspaces. Counts only, never rows.
GRANT SELECT (workspace_id, period_start, reserved_micro_usd, settled_micro_usd) ON usage_accounts TO app_resolver;
CREATE POLICY resolver_totals ON usage_accounts FOR SELECT TO app_resolver USING (true);
GRANT SELECT (workspace_id, deleted_at) ON projects TO app_resolver;
GRANT SELECT (workspace_id, email_normalized, accepted_at, revoked_at, expires_at) ON invitations TO app_resolver;
GRANT SELECT (id, workspace_id, status, config) ON builds TO app_resolver;
CREATE POLICY resolver_totals ON builds FOR SELECT TO app_resolver USING (true);

CREATE FUNCTION app_private.context_namespace() RETURNS uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT w.namespace_id FROM public.workspaces w
   WHERE w.id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
$$;

CREATE FUNCTION app_private.owner_spent_elsewhere(p_period_start timestamptz) RETURNS bigint
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT COALESCE(sum(a.reserved_micro_usd + a.settled_micro_usd), 0)::bigint
    FROM public.usage_accounts a JOIN public.workspaces w ON w.id = a.workspace_id
   WHERE w.namespace_id = app_private.context_namespace()
     AND a.workspace_id <> NULLIF(current_setting('app.workspace_id', true), '')::uuid
     AND a.period_start = p_period_start
$$;

CREATE FUNCTION app_private.owner_totals()
  RETURNS TABLE(projects bigint, people bigint, active_builds bigint)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  WITH ws AS (SELECT w.id FROM public.workspaces w
               WHERE w.namespace_id = app_private.context_namespace() AND w.deleted_at IS NULL)
  SELECT
    (SELECT count(*) FROM public.projects p WHERE p.workspace_id IN (SELECT id FROM ws) AND p.deleted_at IS NULL),
    (SELECT count(*) FROM (
       SELECT m.user_id::text AS who FROM public.memberships m WHERE m.workspace_id IN (SELECT id FROM ws)
       UNION
       SELECT 'invited:' || i.email_normalized FROM public.invitations i
        WHERE i.workspace_id IN (SELECT id FROM ws) AND i.accepted_at IS NULL AND i.revoked_at IS NULL
          AND i.expires_at > now()) people),
    -- A family (a several-language build) is one active build, as in the workspace count (FR-026).
    (SELECT count(DISTINCT COALESCE(b.config->>'family', CAST(b.id AS text))) FROM public.builds b
      WHERE b.workspace_id IN (SELECT id FROM ws) AND b.status IN ('queued', 'running'))
$$;

-- The owner's live workspaces, for the billing sync that suspends or restores all their sites.
CREATE FUNCTION app_private.owner_workspaces() RETURNS SETOF uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT w.id FROM public.workspaces w
   WHERE w.namespace_id = app_private.context_namespace() AND w.deleted_at IS NULL ORDER BY w.id
$$;

-- Who manages the plan of the workspace in context: the person, for their own workspaces; an owner of
-- the organization, for its workspaces.
CREATE FUNCTION app_private.manages_billing(p_user uuid) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.namespaces n
     WHERE n.id = app_private.context_namespace()
       AND ((n.kind = 'user' AND n.user_id = p_user)
         OR (n.kind = 'organization' AND EXISTS (SELECT 1 FROM public.organization_members m
               WHERE m.organization_id = n.organization_id AND m.user_id = p_user AND m.role = 'owner'))))
$$;

-- ---------------------------------------------------------------- the trial, per owner (replaces 0023's)
CREATE OR REPLACE FUNCTION app_private.start_trial(p_workspace uuid, p_amount bigint, p_days integer) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  caller uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
  owner_ns uuid;
  g uuid;
BEGIN
  SELECT w.namespace_id INTO owner_ns FROM public.workspaces w WHERE w.id = p_workspace;
  IF owner_ns IS NULL OR NOT EXISTS (
       SELECT 1 FROM public.namespaces n WHERE n.id = owner_ns
          AND ((n.kind = 'user' AND n.user_id = caller)
            OR (n.kind = 'organization' AND EXISTS (SELECT 1 FROM public.organization_members m
                  WHERE m.organization_id = n.organization_id AND m.user_id = caller AND m.role = 'owner')))) THEN
    RAISE EXCEPTION 'only the person, or an owner of the organization, starts its trial' USING ERRCODE = '42501';
  END IF;
  -- Serialize the owner's and the person's trial starts.
  PERFORM pg_advisory_xact_lock(hashtextextended('billing-account:' || owner_ns::text, 0));
  PERFORM pg_advisory_xact_lock(hashtextextended('trial:' || caller::text, 0));
  IF EXISTS (SELECT 1 FROM public.subscriptions s WHERE s.namespace_id = owner_ns) THEN
    RETURN NULL;  -- an owner that has subscribed has had its trial
  END IF;
  IF EXISTS (SELECT 1 FROM public.entitlement_grants e WHERE e.kind = 'trial' AND e.granted_by = caller) THEN
    RETURN NULL;  -- this person has started a trial before, for any owner
  END IF;
  INSERT INTO public.entitlement_grants(workspace_id, allowance_micro_usd, expires_at, reason, granted_by, kind, once)
  VALUES (p_workspace, p_amount, now() + make_interval(days => p_days), 'Trial', caller, 'trial', true)
  ON CONFLICT (namespace_id) WHERE kind = 'trial' DO NOTHING
  RETURNING id INTO g;
  IF g IS NOT NULL THEN
    INSERT INTO public.audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata)
    VALUES (p_workspace, caller, 'billing.trial_started', 'entitlement', g,
            jsonb_build_object('allowance_micro_usd', p_amount, 'days', p_days));
  END IF;
  RETURN g;
END $$;

DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY['context_namespace()', 'owner_spent_elsewhere(timestamptz)', 'owner_totals()',
                           'owner_workspaces()', 'manages_billing(uuid)'] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;
GRANT SELECT (namespace_id) ON subscriptions TO app_resolver;
GRANT SELECT (namespace_id, kind, granted_by) ON entitlement_grants TO app_resolver;
