REVOKE UPDATE (open_disputes, dispute_lost_at) ON billing_customers FROM app_runtime;
ALTER TABLE billing_customers DROP COLUMN dispute_lost_at;
ALTER TABLE billing_customers DROP COLUMN open_disputes;
DROP INDEX ix_entitlement_trial_owner;
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
    RETURN NULL;
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
