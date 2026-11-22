-- Back to one plan per workspace.
DROP FUNCTION app_private.manages_billing(uuid);
DROP FUNCTION app_private.owner_workspaces();
DROP FUNCTION app_private.owner_totals();
DROP FUNCTION app_private.owner_spent_elsewhere(timestamptz);
DROP FUNCTION app_private.context_namespace();
DROP POLICY resolver_totals ON builds;
DROP POLICY resolver_totals ON usage_accounts;
DROP POLICY tenant ON billing_customers;
DROP POLICY tenant_insert ON billing_customers;
DROP POLICY tenant_update ON billing_customers;
DROP POLICY tenant ON subscriptions;
DROP POLICY tenant_insert ON subscriptions;
DROP POLICY tenant_update ON subscriptions;
DROP POLICY tenant ON entitlement_grants;
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['billing_customers', 'subscriptions'] LOOP
    EXECUTE format('CREATE POLICY tenant ON %I TO app_runtime
      USING (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)
      WITH CHECK (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
  END LOOP;
END $$;
CREATE POLICY tenant ON entitlement_grants FOR SELECT TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['billing_customers', 'subscriptions', 'entitlement_grants'] LOOP
    EXECUTE format('DROP TRIGGER %I ON %I', t || '_namespace', t);
  END LOOP;
END $$;
DROP FUNCTION app_private.billing_namespace();
DROP INDEX uq_entitlement_trial;
CREATE UNIQUE INDEX uq_entitlement_trial ON entitlement_grants(workspace_id) WHERE kind = 'trial';
DROP INDEX uq_billing_customers_namespace;
DROP INDEX ix_subscriptions_namespace;
DROP INDEX ix_entitlement_grants_namespace;
ALTER TABLE billing_customers DROP COLUMN namespace_id;
ALTER TABLE subscriptions DROP COLUMN namespace_id;
ALTER TABLE entitlement_grants DROP COLUMN namespace_id;
CREATE OR REPLACE FUNCTION app_private.start_trial(p_workspace uuid, p_amount bigint, p_days integer) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  caller uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
  g uuid;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.memberships m WHERE m.workspace_id = p_workspace AND m.user_id = caller
                 AND m.role = 'owner') THEN
    RAISE EXCEPTION 'owner role required' USING ERRCODE = '42501';
  END IF;
  IF EXISTS (SELECT 1 FROM public.subscriptions s WHERE s.workspace_id = p_workspace) THEN
    RETURN NULL;  -- a workspace that has subscribed has had its trial
  END IF;
  -- Serialize one person's trial starts, so two workspaces cannot both pass the check below.
  PERFORM pg_advisory_xact_lock(hashtextextended('trial:' || caller::text, 0));
  IF EXISTS (SELECT 1 FROM public.entitlement_grants e WHERE e.kind = 'trial' AND e.granted_by = caller) THEN
    RETURN NULL;  -- this person has started a trial before, in any workspace
  END IF;
  INSERT INTO public.entitlement_grants(workspace_id, allowance_micro_usd, expires_at, reason, granted_by, kind, once)
  VALUES (p_workspace, p_amount, now() + make_interval(days => p_days), 'Trial', caller, 'trial', true)
  ON CONFLICT (workspace_id) WHERE kind = 'trial' DO NOTHING
  RETURNING id INTO g;
  IF g IS NOT NULL THEN
    INSERT INTO public.audit_events(workspace_id, actor_user_id, action, resource_type, resource_id, metadata)
    VALUES (p_workspace, caller, 'billing.trial_started', 'entitlement', g,
            jsonb_build_object('allowance_micro_usd', p_amount, 'days', p_days));
  END IF;
  RETURN g;
END $$;
