-- 0027 per-project access control (01 "per-project ACLs are P1", 09, 21).
--
-- A project is open to its whole workspace (the default, as before) or restricted. A restricted project is
-- visible to workspace admins and owners, and to the members granted access to it as viewer or editor;
-- for them the grant is their role on that project. app_private.project_role is the one rule, used by the
-- API, the event stream, private sites and the row-level backstop on projects below.

ALTER TABLE projects ADD COLUMN access text NOT NULL DEFAULT 'workspace' CHECK (access IN ('workspace', 'restricted'));
ALTER TABLE projects ADD COLUMN access_version integer NOT NULL DEFAULT 1;
GRANT UPDATE (access, access_version) ON projects TO app_runtime;

CREATE TABLE project_members (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  user_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('viewer', 'editor')),
  granted_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, user_id),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id),
  -- Leaving the workspace removes every project grant with the membership.
  FOREIGN KEY (workspace_id, user_id) REFERENCES memberships(workspace_id, user_id) ON DELETE CASCADE
);
CREATE INDEX ix_project_members_user ON project_members(workspace_id, user_id);
CREATE POLICY tenant ON project_members TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE project_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_members FORCE ROW LEVEL SECURITY;
REVOKE ALL ON project_members FROM app_runtime;
GRANT SELECT, INSERT, DELETE ON project_members TO app_runtime;
GRANT UPDATE (role) ON project_members TO app_runtime;

-- The effective role of a user on a project, or NULL when the project is invisible to them.
CREATE POLICY resolver_access ON project_members FOR SELECT TO app_resolver USING (true);
GRANT SELECT (workspace_id, project_id, user_id, role) ON project_members TO app_resolver;
GRANT SELECT (access) ON projects TO app_resolver;
CREATE FUNCTION app_private.project_role(p_workspace uuid, p_project uuid, p_user uuid) RETURNS text
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT CASE
    WHEN m.role IN ('admin', 'owner') THEN m.role
    WHEN p.access = 'workspace' THEN m.role
    ELSE (SELECT g.role FROM public.project_members g
          WHERE g.workspace_id = p_workspace AND g.project_id = p_project AND g.user_id = p_user)
  END
  FROM public.memberships m JOIN public.projects p ON p.workspace_id = m.workspace_id AND p.id = p_project
  WHERE m.workspace_id = p_workspace AND m.user_id = p_user
$$;
ALTER FUNCTION app_private.project_role(uuid, uuid, uuid) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.project_role(uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.project_role(uuid, uuid, uuid) TO app_runtime;

-- Backstop: whenever a request acts for a user (app.user_id set), a restricted project row is invisible
-- unless that user may see it. Workers and system work (no user) see every project of the workspace;
-- the API also checks access explicitly before any project-scoped read or write.
CREATE POLICY project_access ON projects AS RESTRICTIVE TO app_runtime
  USING (
    access = 'workspace'
    OR NULLIF(current_setting('app.user_id', true), '') IS NULL
    OR app_private.project_role(workspace_id, id, NULLIF(current_setting('app.user_id', true), '')::uuid) IS NOT NULL
  )
  WITH CHECK (true);
