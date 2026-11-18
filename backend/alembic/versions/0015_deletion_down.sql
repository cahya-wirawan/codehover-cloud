CREATE OR REPLACE FUNCTION app_private.keep_operator_suspension() RETURNS trigger
  LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF OLD.suspended_reason = 'operator' THEN
    RAISE EXCEPTION 'publication suspended by an operator' USING ERRCODE = '23514',
      CONSTRAINT = 'publications_keep_operator_suspension';
  END IF;
  RETURN OLD;
END $$;

CREATE OR REPLACE FUNCTION app_private.resource_workspace(kind text, resource_id uuid) RETURNS uuid
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  RETURN CASE kind
    WHEN 'project' THEN (SELECT p.workspace_id FROM public.projects p WHERE p.id = resource_id AND p.deleted_at IS NULL)
    WHEN 'build' THEN (SELECT b.workspace_id FROM public.builds b WHERE b.id = resource_id)
    WHEN 'export' THEN (SELECT e.workspace_id FROM public.exports e WHERE e.id = resource_id)
    ELSE NULL END;
END $$;
REVOKE SELECT (build_id) ON exports FROM app_resolver;
REVOKE SELECT (project_id) ON builds FROM app_resolver;

-- Deleted tombstones may share a slug with a live row; give them a unique one so the old constraints fit.
UPDATE workspaces SET slug = left(slug, 38) || '-' || substr(md5(id::text), 1, 8)
 WHERE deleted_at IS NOT NULL AND slug IN (SELECT slug FROM workspaces GROUP BY slug HAVING count(*) > 1);
UPDATE projects SET slug = left(slug, 38) || '-' || substr(md5(id::text), 1, 8)
 WHERE deleted_at IS NOT NULL AND (workspace_id, slug) IN
   (SELECT workspace_id, slug FROM projects GROUP BY workspace_id, slug HAVING count(*) > 1);
DROP INDEX uq_workspaces_live_slug;
ALTER TABLE workspaces ADD CONSTRAINT workspaces_slug_key UNIQUE (slug);
DROP INDEX uq_projects_live_slug;
ALTER TABLE projects ADD CONSTRAINT projects_workspace_id_slug_key UNIQUE (workspace_id, slug);

DROP FUNCTION app_private.claim_deletion(integer);
DROP TABLE deletion_requests;
