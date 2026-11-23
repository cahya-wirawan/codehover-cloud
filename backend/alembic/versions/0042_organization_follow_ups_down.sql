-- Back to 0041: no member details, no billing handover, workspaces stay with their owner.
DROP FUNCTION app_private.move_workspace(uuid, uuid);
DROP FUNCTION app_private.workspace_move_clashes(uuid, uuid);
DROP POLICY resolver_move ON workspaces;
REVOKE UPDATE (namespace_id) ON projects FROM app_resolver;
REVOKE SELECT (name) ON projects FROM app_resolver;
REVOKE UPDATE (namespace_id, updated_at) ON workspaces FROM app_resolver;

DROP FUNCTION app_private.move_billing_anchor(uuid);
DROP POLICY resolver_anchor ON subscriptions;
DROP POLICY resolver_anchor ON billing_customers;
REVOKE UPDATE (workspace_id) ON billing_customers, subscriptions, entitlement_grants FROM app_resolver;
REVOKE SELECT (namespace_id) ON billing_customers FROM app_resolver;
REVOKE SELECT (updated_at) ON subscriptions FROM app_resolver;
CREATE OR REPLACE FUNCTION app_private.billing_namespace() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.namespace_id := (SELECT w.namespace_id FROM public.workspaces w WHERE w.id = NEW.workspace_id);
  ELSE
    NEW.namespace_id := OLD.namespace_id;  -- never moves to another owner
    NEW.workspace_id := OLD.workspace_id;  -- nor to another of its workspaces (security review)
  END IF;
  RETURN NEW;
END $$;

DROP FUNCTION app_private.organization_member_details(uuid);
REVOKE SELECT (name) ON workspaces FROM app_resolver;

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
