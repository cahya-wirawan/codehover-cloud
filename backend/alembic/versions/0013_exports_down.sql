CREATE OR REPLACE FUNCTION app_private.resource_workspace(kind text, resource_id uuid) RETURNS uuid
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  RETURN CASE kind
    WHEN 'project' THEN (SELECT p.workspace_id FROM public.projects p WHERE p.id = resource_id AND p.deleted_at IS NULL)
    WHEN 'build' THEN (SELECT b.workspace_id FROM public.builds b WHERE b.id = resource_id)
    ELSE NULL END;
END $$;
DROP TABLE IF EXISTS exports;
