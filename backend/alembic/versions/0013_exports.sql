-- 0011 static and knowledge-base exports (backlog T16; 06 "/exports", 10).

-- An export package of one ready build, produced by a worker task (stage `export`, work key = export id)
-- and downloaded through an endpoint that authorizes every request; never a public bearer URL. Ready
-- packages expire after 24 hours (410), and the object is purged by the collector (M4).
CREATE TABLE exports (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  format text NOT NULL CHECK (format IN ('static_zip', 'kb_json')),
  variant text NOT NULL DEFAULT 'en-standard',
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'failed')),
  requested_by uuid NOT NULL REFERENCES users(id),
  object_key text CHECK (object_key IS NULL OR object_key ~ '^workspaces/[0-9a-f-]{36}/[A-Za-z0-9._/-]+$'),
  sha256 text CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
  byte_count bigint CHECK (byte_count IS NULL OR byte_count >= 0),
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  expires_at timestamptz,
  CHECK ((status = 'ready') = (object_key IS NOT NULL AND expires_at IS NOT NULL)),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id)
);
CREATE INDEX ix_exports_workspace_created ON exports(workspace_id, created_at);

GRANT SELECT, INSERT, UPDATE ON exports TO app_runtime;
CREATE POLICY tenant ON exports TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE exports ENABLE ROW LEVEL SECURITY;
ALTER TABLE exports FORCE ROW LEVEL SECURITY;

-- Flat route /exports/{eid}: the resolver learns its workspace like projects and builds (05).
CREATE POLICY resolver ON exports FOR SELECT TO app_resolver USING (true);
GRANT SELECT (id, workspace_id) ON exports TO app_resolver;
CREATE OR REPLACE FUNCTION app_private.resource_workspace(kind text, resource_id uuid) RETURNS uuid
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  RETURN CASE kind
    WHEN 'project' THEN (SELECT p.workspace_id FROM public.projects p WHERE p.id = resource_id AND p.deleted_at IS NULL)
    WHEN 'build' THEN (SELECT b.workspace_id FROM public.builds b WHERE b.id = resource_id)
    WHEN 'export' THEN (SELECT e.workspace_id FROM public.exports e WHERE e.id = resource_id)
    ELSE NULL END;
END $$;
