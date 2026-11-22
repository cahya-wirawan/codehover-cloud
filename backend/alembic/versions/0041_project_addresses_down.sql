-- Back to addresses that cannot change.
DROP FUNCTION app_private.sweep_project_slug_redirects();
DROP FUNCTION app_private.rename_project_address(uuid, text);
DROP POLICY resolver_address ON projects;
REVOKE UPDATE (slug, updated_at) ON projects FROM app_resolver;
CREATE OR REPLACE FUNCTION app_private.resolve_site(p_owner text, p_project text)
  RETURNS TABLE (workspace_id uuid, project_id uuid, build_id uuid, visibility text, owner text, project text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.workspace_id, p.project_id, p.build_id, p.visibility, n.handle, pr.slug
  FROM public.namespaces n
  JOIN public.projects pr ON pr.namespace_id = n.id AND pr.slug = p_project AND pr.deleted_at IS NULL
  JOIN public.workspaces w ON w.id = pr.workspace_id AND w.status = 'active'
  JOIN public.publications p ON p.project_id = pr.id AND p.suspended_at IS NULL
  WHERE n.id = COALESCE(
          (SELECT x.id FROM public.namespaces x WHERE x.handle = p_owner),
          (SELECT r.namespace_id FROM public.handle_reservations r WHERE r.handle = p_owner AND r.reserved_until > now()))
    AND (n.organization_id IS NULL OR EXISTS (SELECT 1 FROM public.organizations o
                                               WHERE o.id = n.organization_id AND o.status = 'active'))
$$;
CREATE OR REPLACE FUNCTION app_private.projects_namespace() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  NEW.namespace_id := (SELECT w.namespace_id FROM public.workspaces w WHERE w.id = NEW.workspace_id);
  RETURN NEW;
END $$;
DROP FUNCTION app_private.slug_held_elsewhere(uuid, text, uuid);
DROP TABLE project_slug_redirects;
