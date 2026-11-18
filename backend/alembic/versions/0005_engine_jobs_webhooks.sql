-- 0005 engine output, job notifications and GitHub webhooks (backlog T07-T09; 05, 06).

-- ---------------------------------------------------------------- engine output
CREATE TABLE symbols (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  source_file_id uuid NOT NULL,
  symbol_key text NOT NULL CHECK (char_length(symbol_key) BETWEEN 1 AND 1024),
  name text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('function', 'method', 'class', 'interface', 'type', 'variable', 'module')),
  parent_key text,
  -- Lines 1-based inclusive; columns 0-based, end-exclusive, Unicode scalar offsets (02, 06).
  start_line integer NOT NULL CHECK (start_line >= 1),
  end_line integer NOT NULL,
  start_column integer NOT NULL CHECK (start_column >= 0),
  end_column integer NOT NULL CHECK (end_column >= 0),
  signature text,
  rank double precision NOT NULL DEFAULT 0,
  card jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(card) = 'object'),
  CHECK (end_line >= start_line),
  UNIQUE (build_id, symbol_key),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id),
  FOREIGN KEY (workspace_id, source_file_id) REFERENCES source_files(workspace_id, id)
);
CREATE INDEX ix_symbols_build_file ON symbols(build_id, source_file_id);
CREATE INDEX ix_symbols_build_name ON symbols(build_id, name);

CREATE TABLE reference_edges (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  from_symbol_id uuid,
  to_symbol_id uuid NOT NULL,
  source_file_id uuid NOT NULL,
  line integer NOT NULL CHECK (line >= 1),
  "column" integer NOT NULL CHECK ("column" >= 0),
  -- 'name' = matched by name only (never presented as type-confirmed); ambiguous matches share a group.
  resolution text NOT NULL CHECK (resolution IN ('name', 'index')),
  ambiguity_group text,
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id),
  FOREIGN KEY (workspace_id, to_symbol_id) REFERENCES symbols(workspace_id, id),
  FOREIGN KEY (workspace_id, from_symbol_id) REFERENCES symbols(workspace_id, id),
  FOREIGN KEY (workspace_id, source_file_id) REFERENCES source_files(workspace_id, id)
);
CREATE INDEX ix_reference_edges_to ON reference_edges(build_id, to_symbol_id);
CREATE INDEX ix_reference_edges_from ON reference_edges(build_id, from_symbol_id);

-- ---------------------------------------------------------------- job notifications
ALTER TABLE build_tasks ADD COLUMN notified_at timestamptz;
GRANT SELECT (notified_at), UPDATE (notified_at) ON build_tasks TO app_system;

-- ---------------------------------------------------------------- webhooks (no tenant content)
CREATE TABLE webhook_receipts (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  provider text NOT NULL CHECK (provider IN ('github', 'stripe')),
  provider_event_id text NOT NULL CHECK (char_length(provider_event_id) BETWEEN 1 AND 200),
  event_type text NOT NULL,
  payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
  state text NOT NULL DEFAULT 'received' CHECK (state IN ('received', 'processed', 'ignored', 'failed')),
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  UNIQUE (provider, provider_event_id)
);

-- ---------------------------------------------------------------- privileges and RLS
GRANT SELECT, INSERT, UPDATE, DELETE ON symbols, reference_edges TO app_runtime;
GRANT SELECT, INSERT, UPDATE ON webhook_receipts TO app_runtime;
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['symbols', 'reference_edges'] LOOP
    EXECUTE format('CREATE POLICY tenant ON %I TO app_runtime
      USING (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)
      WITH CHECK (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['symbols', 'reference_edges', 'webhook_receipts'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
-- Receipts hold only provider event IDs and digests; signature verification gates every write.
CREATE POLICY receipts ON webhook_receipts TO app_runtime USING (true) WITH CHECK (true);

-- Installation lifecycle from a signed webhook: no workspace context exists, so a resolver-owned
-- function updates every connection of that installation and returns how many changed.
CREATE POLICY resolver_lifecycle ON repository_connections FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_lifecycle_update ON repository_connections FOR UPDATE TO app_resolver
  USING (true) WITH CHECK (true);
GRANT SELECT (id, installation_id, status), UPDATE (status, updated_at) ON repository_connections TO app_resolver;

CREATE FUNCTION app_private.set_installation_status(p_installation_id bigint, p_status text) RETURNS integer
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE changed integer;
BEGIN
  IF p_status NOT IN ('active', 'suspended', 'revoked') THEN
    RAISE EXCEPTION 'invalid connection status' USING ERRCODE = '22023';
  END IF;
  UPDATE public.repository_connections SET status = p_status, updated_at = now()
    WHERE installation_id = p_installation_id
      AND status <> 'revoked'           -- a revoked connection is never revived by a webhook
      AND status <> p_status;
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed;
END $$;
ALTER FUNCTION app_private.set_installation_status(bigint, text) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.set_installation_status(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.set_installation_status(bigint, text) TO app_runtime;
