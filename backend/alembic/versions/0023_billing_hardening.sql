-- 0023 billing hardening (M5 security review; 11).

-- A trial is once per person as well as once per workspace: creating workspaces must not mint trials.
CREATE INDEX ix_entitlement_trial_owner ON entitlement_grants(granted_by) WHERE kind = 'trial';
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
ALTER FUNCTION app_private.start_trial(uuid, bigint, integer) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.start_trial(uuid, bigint, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.start_trial(uuid, bigint, integer) TO app_runtime;

-- Disputes one by one: generation stays blocked while any dispute is open, and after a lost dispute until
-- an operator clears it (`python -m app.ops.billing clear-dispute`).
ALTER TABLE billing_customers ADD COLUMN open_disputes text[] NOT NULL DEFAULT '{}'
  CHECK (cardinality(open_disputes) <= 100);
ALTER TABLE billing_customers ADD COLUMN dispute_lost_at timestamptz;
GRANT UPDATE (open_disputes, dispute_lost_at) ON billing_customers TO app_runtime;
