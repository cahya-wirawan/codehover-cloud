-- 0011 publication hardening (T15 security review).

-- Delivery resolves only publications of live projects in active workspaces, like every member route
-- (access.py requires an active workspace; deleted projects are gone from every listing).
GRANT SELECT (id, status) ON workspaces TO app_resolver;
CREATE OR REPLACE FUNCTION app_private.resolve_publication(slug text)
  RETURNS TABLE (workspace_id uuid, project_id uuid, build_id uuid, visibility text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.workspace_id, p.project_id, p.build_id, p.visibility
  FROM public.publications p
  JOIN public.workspaces w ON w.id = p.workspace_id AND w.status = 'active'
  JOIN public.projects pr ON pr.id = p.project_id AND pr.deleted_at IS NULL
  WHERE p.public_slug = $1 AND p.suspended_at IS NULL  -- $1: projects also has a column named slug
$$;
