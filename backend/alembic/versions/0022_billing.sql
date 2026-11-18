-- 0022 billing: customers, subscriptions, trials and billing suspensions (M5; 05, 06, 11, A20).

-- The payment provider is the source of truth for payment and subscription status; these tables are the
-- reconciled snapshot service access is computed from (11). Webhooks only say "something changed": the
-- processor always fetches the subscription from the provider, so event order and duplicates do not
-- matter, and a success redirect never grants anything (A20).

CREATE TABLE billing_customers (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL UNIQUE REFERENCES workspaces(id),
  provider text NOT NULL DEFAULT 'stripe' CHECK (provider = 'stripe'),
  provider_customer_id text NOT NULL UNIQUE CHECK (provider_customer_id ~ '^[A-Za-z0-9_]{1,255}$'),
  disputed_at timestamptz,  -- an open payment dispute blocks new generation until it closes (11)
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id)
);

CREATE TABLE subscriptions (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  provider_subscription_id text NOT NULL UNIQUE CHECK (provider_subscription_id ~ '^[A-Za-z0-9_]{1,255}$'),
  provider_customer_id text NOT NULL,
  plan_key text NOT NULL CHECK (plan_key IN ('pro', 'team')),
  status text NOT NULL CHECK (status IN ('incomplete', 'incomplete_expired', 'trialing', 'active', 'past_due',
                                         'canceled', 'unpaid', 'paused')),
  current_period_start timestamptz,
  current_period_end timestamptz,
  cancel_at_period_end boolean NOT NULL DEFAULT false,
  ended_at timestamptz,
  past_due_since timestamptz,
  synced_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id)
);
CREATE INDEX ix_subscriptions_workspace ON subscriptions(workspace_id, status);

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['billing_customers', 'subscriptions'] LOOP
    EXECUTE format('CREATE POLICY tenant ON %I TO app_runtime
      USING (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)
      WITH CHECK (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('REVOKE ALL ON %I FROM app_runtime', t);
  END LOOP;
END $$;
GRANT SELECT, INSERT, UPDATE (disputed_at) ON billing_customers TO app_runtime;
GRANT SELECT, INSERT, UPDATE ON subscriptions TO app_runtime;

-- A webhook names a provider customer; its workspace comes from our own record of who created that
-- customer, never from the event's metadata (06: do not trust webhook-supplied workspace IDs).
CREATE POLICY resolver ON billing_customers FOR SELECT TO app_resolver USING (true);
GRANT SELECT (workspace_id, provider_customer_id) ON billing_customers TO app_resolver;
CREATE FUNCTION app_private.billing_workspace(p_customer text) RETURNS uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.workspace_id FROM public.billing_customers c WHERE c.provider_customer_id = p_customer
$$;

-- Trials: one per workspace, never after a subscription, allowance counted once (in the month it starts).
ALTER TABLE entitlement_grants ADD COLUMN kind text NOT NULL DEFAULT 'manual' CHECK (kind IN ('manual', 'trial'));
ALTER TABLE entitlement_grants ADD COLUMN once boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX uq_entitlement_trial ON entitlement_grants(workspace_id) WHERE kind = 'trial';
CREATE POLICY resolver_trial ON memberships FOR SELECT TO app_resolver USING (true);
GRANT SELECT (workspace_id, user_id, role) ON memberships TO app_resolver;
CREATE POLICY resolver_trial ON subscriptions FOR SELECT TO app_resolver USING (true);
GRANT SELECT (workspace_id) ON subscriptions TO app_resolver;
GRANT INSERT ON audit_events TO app_resolver;
CREATE FUNCTION app_private.start_trial(p_workspace uuid, p_amount bigint, p_days integer) RETURNS uuid
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

ALTER FUNCTION app_private.billing_workspace(text) OWNER TO app_resolver;
ALTER FUNCTION app_private.start_trial(uuid, bigint, integer) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.billing_workspace(text), app_private.start_trial(uuid, bigint, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.billing_workspace(text), app_private.start_trial(uuid, bigint, integer) TO app_runtime;

-- Hosting suspended by the plan lifecycle (11: ended or long past due); lifted when billing recovers.
ALTER TABLE publications DROP CONSTRAINT publications_suspended_reason_check;
ALTER TABLE publications ADD CONSTRAINT publications_suspended_reason_check
  CHECK (suspended_reason IS NULL OR suspended_reason IN ('connection_inactive', 'repository_revoked', 'operator', 'billing'));

-- Stripe receipts keep the few identifiers processing needs (no names, emails or card data) and may be
-- rejected when the event's metadata contradicts our records.
ALTER TABLE webhook_receipts ADD COLUMN details jsonb;
ALTER TABLE webhook_receipts DROP CONSTRAINT webhook_receipts_state_check;
ALTER TABLE webhook_receipts ADD CONSTRAINT webhook_receipts_state_check
  CHECK (state IN ('received', 'processed', 'ignored', 'failed', 'rejected'));
