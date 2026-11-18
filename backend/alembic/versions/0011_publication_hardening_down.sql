CREATE OR REPLACE FUNCTION app_private.resolve_publication(slug text)
  RETURNS TABLE (workspace_id uuid, project_id uuid, build_id uuid, visibility text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.workspace_id, p.project_id, p.build_id, p.visibility
  FROM public.publications p WHERE p.public_slug = slug AND p.suspended_at IS NULL
$$;
REVOKE SELECT (status) ON workspaces FROM app_resolver;
