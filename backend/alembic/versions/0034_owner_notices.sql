-- 0034 owner notices (11 "Owner notice", 12, 22).
--
-- One row per state change worth an email to the workspace's owners and admins. Rows hold identifiers and
-- reason codes only; names and addresses are read when the email is written, so a purge or rename needs no
-- second copy scrubbed. `dedupe_key` makes recording idempotent: the same change seen twice (a repeated
-- webhook, a re-run sync) records one notice.

CREATE TABLE owner_notices (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL CHECK (kind IN ('site_suspended', 'billing_past_due', 'dispute_opened', 'dispute_lost')),
  dedupe_key text NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 200),
  details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'sending', 'sent', 'failed', 'skipped')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text CHECK (last_error IS NULL OR last_error ~ '^[a-z_]{1,64}$'),
  recipients integer CHECK (recipients IS NULL OR recipients >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, dedupe_key)
);
CREATE INDEX ix_owner_notices_due ON owner_notices(next_attempt_at) WHERE state IN ('pending', 'sending');
CREATE INDEX ix_owner_notices_workspace ON owner_notices(workspace_id, created_at DESC);

CREATE POLICY tenant ON owner_notices TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE owner_notices ENABLE ROW LEVEL SECURITY;
ALTER TABLE owner_notices FORCE ROW LEVEL SECURITY;
REVOKE ALL ON owner_notices FROM app_runtime;
-- DELETE: the retention sweep (90 days) and the workspace purge.
GRANT SELECT, INSERT, DELETE ON owner_notices TO app_runtime;
GRANT UPDATE (state, attempts, next_attempt_at, last_error, recipients, sent_at) ON owner_notices TO app_runtime;

-- Every path that stops a site (the connection and repository triggers of 0010, an operator takedown,
-- billing, a license check) ends in an UPDATE of publications, so one statement-level trigger records the
-- notice for all of them: one notice per workspace and reason per statement, naming the projects, so a
-- billing suspension of many sites sends one email. It runs as app_resolver, which may only insert notices and merge their project lists.
-- Several statements of one transaction (one per revoked repository, say) merge into its notice.
CREATE POLICY resolver_insert ON owner_notices FOR INSERT TO app_resolver WITH CHECK (true);
CREATE POLICY resolver_merge ON owner_notices FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);
GRANT INSERT (workspace_id, kind, dedupe_key, details), UPDATE (details) ON owner_notices TO app_resolver;
CREATE FUNCTION app_private.notice_site_suspensions() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  INSERT INTO public.owner_notices(workspace_id, kind, dedupe_key, details)
  SELECT n.workspace_id, 'site_suspended',
         'site_suspended:' || n.suspended_reason || ':' || pg_current_xact_id()::text,
         jsonb_build_object('reason', n.suspended_reason,
                            'project_ids', jsonb_agg(DISTINCT n.project_id))
  FROM new_rows n JOIN old_rows o ON o.id = n.id
  WHERE n.suspended_at IS NOT NULL AND o.suspended_at IS NULL
  GROUP BY n.workspace_id, n.suspended_reason
  ON CONFLICT (workspace_id, dedupe_key) DO UPDATE
    SET details = jsonb_set(owner_notices.details, '{project_ids}',
      (SELECT jsonb_agg(DISTINCT e.v) FROM jsonb_array_elements(
         (owner_notices.details->'project_ids') || (EXCLUDED.details->'project_ids')) AS e(v)));
  RETURN NULL;
END $$;
ALTER FUNCTION app_private.notice_site_suspensions() OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.notice_site_suspensions() FROM PUBLIC;
CREATE TRIGGER publications_notice_suspension AFTER UPDATE ON publications
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION app_private.notice_site_suspensions();

-- The worker finds due notices across tenants, then sends each inside its workspace's context.
CREATE POLICY resolver ON owner_notices FOR SELECT TO app_resolver USING (true);
GRANT SELECT (id, workspace_id, dedupe_key, details, state, next_attempt_at) ON owner_notices TO app_resolver;
CREATE FUNCTION app_private.due_owner_notices(p_limit integer)
  RETURNS TABLE(workspace_id uuid, notice_id uuid)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT n.workspace_id, n.id FROM public.owner_notices n
  WHERE n.state IN ('pending', 'sending') AND n.next_attempt_at <= now()
  ORDER BY n.next_attempt_at LIMIT least(greatest(p_limit, 1), 500)
$$;
ALTER FUNCTION app_private.due_owner_notices(integer) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.due_owner_notices(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.due_owner_notices(integer) TO app_runtime;
