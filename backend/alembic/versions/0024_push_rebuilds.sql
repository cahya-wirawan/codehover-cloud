-- 0024 push rebuilds (M6, T21; 01 FR-025, 06, 09).
--
-- A project's admin chooses what a push to the tracked branch does: nothing (default), take a snapshot,
-- or build. The person who set the policy is recorded: work runs on their behalf and stops if they lose
-- the admin role. Pushes are debounced into one pending request per project and deduplicated by commit;
-- nothing a push starts is ever published.

ALTER TABLE projects ADD COLUMN push_policy text NOT NULL DEFAULT 'off'
  CHECK (push_policy IN ('off', 'snapshot', 'build'));
ALTER TABLE projects ADD COLUMN push_cap_micro_usd bigint NOT NULL DEFAULT 0 CHECK (push_cap_micro_usd >= 0);
ALTER TABLE projects ADD COLUMN push_policy_by uuid REFERENCES users(id);
ALTER TABLE projects ADD COLUMN push_policy_version integer NOT NULL DEFAULT 1;
ALTER TABLE projects ADD CONSTRAINT projects_push_policy_owner CHECK (push_policy = 'off' OR push_policy_by IS NOT NULL);

CREATE TABLE push_requests (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  project_id uuid NOT NULL,
  commit_sha text NOT NULL CHECK (commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
  ref text NOT NULL CHECK (length(ref) BETWEEN 1 AND 255),
  pushes integer NOT NULL DEFAULT 1 CHECK (pushes >= 1),  -- deliveries coalesced into this request
  first_received_at timestamptz NOT NULL DEFAULT now(),
  due_at timestamptz NOT NULL,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'acquiring', 'done', 'skipped', 'failed')),
  outcome text CHECK (outcome IS NULL OR outcome ~ '^[a-z_]{1,64}$'),
  snapshot_id uuid,
  build_id uuid,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id),
  FOREIGN KEY (workspace_id, snapshot_id) REFERENCES snapshots(workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id)
);
-- At most one waiting and one in-flight request per project: later pushes update the waiting one
-- (debounce) instead of queueing more work, also while an earlier commit is still being acquired.
CREATE UNIQUE INDEX uq_push_requests_pending ON push_requests(project_id) WHERE state = 'pending';
CREATE UNIQUE INDEX uq_push_requests_acquiring ON push_requests(project_id) WHERE state = 'acquiring';
CREATE INDEX ix_push_requests_project ON push_requests(workspace_id, project_id, first_received_at DESC);
CREATE INDEX ix_push_requests_due ON push_requests(due_at) WHERE state IN ('pending', 'acquiring');

CREATE POLICY tenant ON push_requests TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE push_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE push_requests FORCE ROW LEVEL SECURITY;
REVOKE ALL ON push_requests FROM app_runtime;
GRANT SELECT, INSERT, UPDATE ON push_requests TO app_runtime;
GRANT UPDATE (push_policy, push_cap_micro_usd, push_policy_by, push_policy_version) ON projects TO app_runtime;

-- A push names an installation and a repository; the projects it concerns are found across tenants by
-- this resolver function (identifiers only), never from anything the payload claims about workspaces.
CREATE POLICY resolver_push ON repositories FOR SELECT TO app_resolver USING (true);
GRANT SELECT (id, workspace_id, connection_id, provider_repo_id, access_revoked_at) ON repositories TO app_resolver;
GRANT SELECT (id, workspace_id, repository_id, tracked_ref, push_policy, deleted_at) ON projects TO app_resolver;
GRANT SELECT (id, workspace_id, installation_id, status) ON repository_connections TO app_resolver;
CREATE FUNCTION app_private.push_targets(p_installation bigint, p_repo bigint, p_branch text)
  RETURNS TABLE(workspace_id uuid, project_id uuid)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.workspace_id, p.id FROM public.projects p
  JOIN public.repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id
  JOIN public.repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id
  WHERE c.installation_id = p_installation AND c.status = 'active' AND r.provider_repo_id = p_repo
    AND r.access_revoked_at IS NULL AND p.deleted_at IS NULL AND p.tracked_ref = p_branch
    AND p.push_policy <> 'off'
$$;

-- The worker finds due requests across tenants the same way, then works inside each workspace's context.
CREATE POLICY resolver ON push_requests FOR SELECT TO app_resolver USING (true);
GRANT SELECT (id, workspace_id, due_at, state) ON push_requests TO app_resolver;
CREATE FUNCTION app_private.due_push_requests(p_limit integer)
  RETURNS TABLE(workspace_id uuid, request_id uuid)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT q.workspace_id, q.id FROM public.push_requests q
  WHERE q.state IN ('pending', 'acquiring') AND q.due_at <= now()
  ORDER BY q.due_at LIMIT least(greatest(p_limit, 1), 500)
$$;

ALTER FUNCTION app_private.push_targets(bigint, bigint, text) OWNER TO app_resolver;
ALTER FUNCTION app_private.due_push_requests(integer) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.push_targets(bigint, bigint, text), app_private.due_push_requests(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.push_targets(bigint, bigint, text), app_private.due_push_requests(integer) TO app_runtime;
