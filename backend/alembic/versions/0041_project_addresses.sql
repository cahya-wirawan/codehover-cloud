-- 0041 changing a project's address (02 S19, 06 PUT /projects/{pid}/address).
--
-- A project's slug is the second half of its site address, /sites/<owner>/<project> (0039). An admin may
-- change it. The old address keeps leading to the project for 90 days (the three newest per project),
-- like a renamed owner's handle (0038), so links and MCP clients move without breaking. While it lasts
-- only a project of the same workspace may take the old address (which ends the redirect); another
-- workspace of the same owner gets SLUG_TAKEN, so an organization's workspace admins cannot take each
-- other's links (security review). Redirects stay inside one owner's namespace.

CREATE TABLE project_slug_redirects (
  namespace_id uuid NOT NULL REFERENCES namespaces(id),
  slug text NOT NULL CHECK (slug ~ '^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$'),
  project_id uuid NOT NULL REFERENCES projects(id),
  released_at timestamptz NOT NULL,
  reserved_until timestamptz NOT NULL,
  PRIMARY KEY (namespace_id, slug),
  CHECK (reserved_until > released_at)
);
CREATE INDEX ix_project_slug_redirects_project ON project_slug_redirects(project_id, released_at DESC);
ALTER TABLE project_slug_redirects ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_slug_redirects FORCE ROW LEVEL SECURITY;
REVOKE ALL ON project_slug_redirects FROM app_runtime;
CREATE POLICY resolver ON project_slug_redirects TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON project_slug_redirects TO app_resolver;

-- Whether a redirect of the owner still holds `p_slug` for a project of another workspace.
CREATE FUNCTION app_private.slug_held_elsewhere(p_namespace uuid, p_slug text, p_workspace uuid) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT EXISTS (SELECT 1 FROM public.project_slug_redirects r JOIN public.projects pr ON pr.id = r.project_id
                  WHERE r.namespace_id = p_namespace AND r.slug = p_slug AND r.reserved_until > now()
                    AND pr.workspace_id <> p_workspace)
$$;

-- A new project may take a slug whose redirect belongs to its own workspace (ending it), never one of
-- another workspace's while it lasts.
CREATE OR REPLACE FUNCTION app_private.projects_namespace() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  NEW.namespace_id := (SELECT w.namespace_id FROM public.workspaces w WHERE w.id = NEW.workspace_id);
  IF app_private.slug_held_elsewhere(NEW.namespace_id, NEW.slug, NEW.workspace_id) THEN
    RAISE EXCEPTION 'this address was used by another workspace''s project recently' USING ERRCODE = '23505',
      CONSTRAINT = 'uq_projects_namespace_slug';
  END IF;
  DELETE FROM public.project_slug_redirects r WHERE r.namespace_id = NEW.namespace_id AND r.slug = NEW.slug;
  RETURN NEW;
END $$;

-- Rename a live project's address; the caller (app.user_id) must be an admin or owner of its workspace.
-- Returns the new slug; a slug another live project of the owner uses is a unique violation.
GRANT UPDATE (slug, updated_at) ON projects TO app_resolver;
CREATE POLICY resolver_address ON projects FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);
CREATE FUNCTION app_private.rename_project_address(p_project uuid, p_slug text) RETURNS text
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
  p record;
BEGIN
  SELECT x.id, x.workspace_id, x.namespace_id, x.slug INTO p FROM public.projects x
   WHERE x.id = p_project AND x.deleted_at IS NULL FOR UPDATE;
  IF NOT FOUND OR uid IS NULL OR NOT EXISTS (
       SELECT 1 FROM public.memberships m WHERE m.workspace_id = p.workspace_id AND m.user_id = uid
          AND m.role IN ('admin', 'owner')) THEN
    RAISE EXCEPTION 'only a workspace admin changes a project''s address' USING ERRCODE = '42501';
  END IF;
  IF p.slug = p_slug THEN
    RETURN p_slug;
  END IF;
  IF app_private.slug_held_elsewhere(p.namespace_id, p_slug, p.workspace_id) THEN
    RAISE EXCEPTION 'this address was used by another workspace''s project recently' USING ERRCODE = '23505',
      CONSTRAINT = 'uq_projects_namespace_slug';
  END IF;
  UPDATE public.projects x SET slug = p_slug, updated_at = now() WHERE x.id = p.id;
  DELETE FROM public.project_slug_redirects r WHERE r.namespace_id = p.namespace_id AND r.slug = p_slug;
  INSERT INTO public.project_slug_redirects(namespace_id, slug, project_id, released_at, reserved_until)
  VALUES (p.namespace_id, p.slug, p.id, clock_timestamp(), clock_timestamp() + interval '90 days')
  ON CONFLICT (namespace_id, slug) DO UPDATE SET project_id = EXCLUDED.project_id,
    released_at = EXCLUDED.released_at, reserved_until = EXCLUDED.reserved_until;
  DELETE FROM public.project_slug_redirects r WHERE r.project_id = p.id
     AND r.slug IN (SELECT y.slug FROM public.project_slug_redirects y WHERE y.project_id = p.id
                     ORDER BY y.released_at DESC, y.slug OFFSET 3);
  RETURN p_slug;
END $$;

-- Resolving an address (replaces 0039's): a live project's slug first, else a redirect of the owner
-- (renamed project), each under the owner's current or still-reserved old handle. `project` is the
-- project's current slug, so callers can move to the canonical address.
CREATE OR REPLACE FUNCTION app_private.resolve_site(p_owner text, p_project text)
  RETURNS TABLE (workspace_id uuid, project_id uuid, build_id uuid, visibility text, owner text, project text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  WITH owner_ns AS (
    SELECT n.id, n.handle FROM public.namespaces n
     WHERE n.id = COALESCE(
             (SELECT x.id FROM public.namespaces x WHERE x.handle = p_owner),
             (SELECT r.namespace_id FROM public.handle_reservations r WHERE r.handle = p_owner AND r.reserved_until > now()))
       AND (n.organization_id IS NULL OR EXISTS (SELECT 1 FROM public.organizations o
                                                  WHERE o.id = n.organization_id AND o.status = 'active'))),
  target AS (
    SELECT COALESCE(
      (SELECT pr.id FROM public.projects pr, owner_ns
        WHERE pr.namespace_id = owner_ns.id AND pr.slug = p_project AND pr.deleted_at IS NULL),
      (SELECT r.project_id FROM public.project_slug_redirects r, owner_ns
        WHERE r.namespace_id = owner_ns.id AND r.slug = p_project AND r.reserved_until > now())) AS id)
  SELECT p.workspace_id, p.project_id, p.build_id, p.visibility, owner_ns.handle, pr.slug
  FROM target
  JOIN public.projects pr ON pr.id = target.id AND pr.deleted_at IS NULL
  JOIN owner_ns ON owner_ns.id = pr.namespace_id
  JOIN public.workspaces w ON w.id = pr.workspace_id AND w.status = 'active'
  JOIN public.publications p ON p.project_id = pr.id AND p.suspended_at IS NULL
$$;

-- The hourly retention sweep removes ended redirects.
CREATE FUNCTION app_private.sweep_project_slug_redirects() RETURNS bigint
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n bigint;
BEGIN
  DELETE FROM public.project_slug_redirects WHERE reserved_until < now();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY['rename_project_address(uuid, text)', 'sweep_project_slug_redirects()',
                           'slug_held_elsewhere(uuid, text, uuid)'] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;
