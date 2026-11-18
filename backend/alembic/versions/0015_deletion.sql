-- 0015 project, workspace and account deletion with tombstones (backlog T17; 01 FR-021, 05, 08, A22).

-- A deletion request is the tombstone: identifiers only, never names or content. It is written in the
-- same transaction that stops delivery (projects.deleted_at, workspaces.status = 'deleting', users.status
-- = 'deleted') and fences the target's tasks. Purge runs after `not_before`, once every fenced lease has
-- expired, and is retried until it completes. A copy of every tombstone is also kept in object storage,
-- so a database restored from an older backup can reapply deletions before it serves traffic (22).
CREATE TABLE deletion_requests (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  scope text NOT NULL CHECK (scope IN ('project', 'workspace', 'account')),
  workspace_id uuid,
  project_id uuid,
  user_id uuid,
  requested_by uuid,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'purging', 'completed')),
  not_before timestamptz NOT NULL DEFAULT now() + interval '5 minutes',
  lease_until timestamptz,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  error_code text CHECK (error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$'),
  requested_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CHECK (
    (scope = 'project' AND workspace_id IS NOT NULL AND project_id IS NOT NULL AND user_id IS NULL)
    OR (scope = 'workspace' AND workspace_id IS NOT NULL AND project_id IS NULL AND user_id IS NULL)
    OR (scope = 'account' AND user_id IS NOT NULL AND workspace_id IS NULL AND project_id IS NULL)
  ),
  CHECK ((state = 'completed') = (completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_deletion_project ON deletion_requests(project_id) WHERE scope = 'project';
CREATE UNIQUE INDEX uq_deletion_workspace ON deletion_requests(workspace_id) WHERE scope = 'workspace';
CREATE UNIQUE INDEX uq_deletion_account ON deletion_requests(user_id) WHERE scope = 'account';
CREATE INDEX ix_deletion_due ON deletion_requests(not_before) WHERE state <> 'completed';

GRANT SELECT, INSERT, UPDATE ON deletion_requests TO app_runtime;
-- Workspace-scoped requests are visible in their workspace; an account request only to its user.
CREATE POLICY tenant ON deletion_requests TO app_runtime
  USING (
    (workspace_id IS NOT NULL AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    OR (workspace_id IS NULL AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  )
  WITH CHECK (
    (workspace_id IS NOT NULL AND workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
    OR (workspace_id IS NULL AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  );
ALTER TABLE deletion_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE deletion_requests FORCE ROW LEVEL SECURITY;

-- The purger learns which request is due without seeing any tenant data: identifiers and a lease.
CREATE POLICY resolver ON deletion_requests TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT (id, scope, workspace_id, project_id, user_id, state, not_before, lease_until, attempts, requested_at),
  UPDATE (state, lease_until, attempts) ON deletion_requests TO app_resolver;
CREATE FUNCTION app_private.claim_deletion(p_lease_seconds integer)
  RETURNS TABLE (id uuid, scope text, workspace_id uuid, project_id uuid, user_id uuid, attempts integer,
                 requested_at timestamptz)
  LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  UPDATE public.deletion_requests d
     SET state = 'purging', attempts = d.attempts + 1,
         lease_until = now() + make_interval(secs => p_lease_seconds)
   WHERE d.id = (
     SELECT q.id FROM public.deletion_requests q
      WHERE q.state <> 'completed' AND q.not_before <= now()
        AND (q.lease_until IS NULL OR q.lease_until < now())
      ORDER BY q.not_before, q.id
      LIMIT 1 FOR UPDATE SKIP LOCKED)
  RETURNING d.id, d.scope, d.workspace_id, d.project_id, d.user_id, d.attempts, d.requested_at
$$;
ALTER FUNCTION app_private.claim_deletion(integer) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.claim_deletion(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.claim_deletion(integer) TO app_runtime;

-- Deleted projects free their slug; workspaces too (the old rows stay as minimal tombstones).
ALTER TABLE projects DROP CONSTRAINT projects_workspace_id_slug_key;
CREATE UNIQUE INDEX uq_projects_live_slug ON projects(workspace_id, slug) WHERE deleted_at IS NULL;
ALTER TABLE workspaces DROP CONSTRAINT workspaces_slug_key;
CREATE UNIQUE INDEX uq_workspaces_live_slug ON workspaces(slug) WHERE deleted_at IS NULL;

-- Flat routes of a deleted project's builds and exports are not found, like the project itself.
GRANT SELECT (project_id) ON builds TO app_resolver;
GRANT SELECT (build_id) ON exports TO app_resolver;
CREATE OR REPLACE FUNCTION app_private.resource_workspace(kind text, resource_id uuid) RETURNS uuid
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  RETURN CASE kind
    WHEN 'project' THEN (SELECT p.workspace_id FROM public.projects p WHERE p.id = resource_id AND p.deleted_at IS NULL)
    WHEN 'build' THEN (
      SELECT b.workspace_id FROM public.builds b
        JOIN public.projects p ON p.id = b.project_id AND p.deleted_at IS NULL
       WHERE b.id = resource_id)
    WHEN 'export' THEN (
      SELECT e.workspace_id FROM public.exports e
        JOIN public.builds b ON b.id = e.build_id
        JOIN public.projects p ON p.id = b.project_id AND p.deleted_at IS NULL
       WHERE e.id = resource_id)
    ELSE NULL END;
END $$;

-- Purging a deleted project removes its publication row too, even an operator-suspended one: the
-- project can no longer be published, so the takedown has nothing left to protect.
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
