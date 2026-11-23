-- 0043 hosted MCP follow-ups (06 "/mcp/{owner}/{project}", "/oauth").
--
-- 1. Client ID metadata documents (MCP 2025-11-25): a client's `client_id` may be the https URL of its
--    metadata document. Sign-in fetches it and keeps it as a client row named by that URL (refreshed
--    after an hour), so codes and tokens work exactly as for registered clients.
-- 2. MCP sessions: `initialize` opens one (`Mcp-Session-Id`; only its SHA-256 digest is stored), bound to
--    the site's project and to the person whose token opened it (none on a public site). A session holds
--    up to 50 resource subscriptions; its GET stream tells the client when the site's published build
--    changes. Sessions end after a day without use (at most a week), and are swept hourly.

-- ---------------------------------------------------------------- 1. client metadata documents
ALTER TABLE oauth_clients DROP CONSTRAINT oauth_clients_client_id_check;
ALTER TABLE oauth_clients ADD CONSTRAINT oauth_clients_client_id_check CHECK (
  client_id ~ '^chc_[A-Za-z0-9_-]{22,64}$'
  OR (client_id ~ '^https://[A-Za-z0-9.-]+(:443)?/[^#[:space:]]+$' AND char_length(client_id) <= 512));
ALTER TABLE oauth_clients ADD COLUMN fetched_at timestamptz;
ALTER TABLE oauth_clients ADD CONSTRAINT oauth_clients_fetched CHECK ((fetched_at IS NOT NULL) = (client_id LIKE 'https://%'));
GRANT UPDATE (client_name, redirect_uris, fetched_at) ON oauth_clients TO app_resolver;

-- Keep (or refresh) the client of a fetched metadata document; returns its row ID.
CREATE FUNCTION app_private.oauth_metadata_client(p_url text, p_name text, p_uris text[]) RETURNS uuid
  LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  INSERT INTO public.oauth_clients(client_id, client_name, redirect_uris, fetched_at)
  VALUES (p_url, p_name, p_uris, now())
  ON CONFLICT (client_id) DO UPDATE SET client_name = EXCLUDED.client_name,
    redirect_uris = EXCLUDED.redirect_uris, fetched_at = EXCLUDED.fetched_at
  RETURNING id
$$;

DROP FUNCTION app_private.oauth_client(text);
CREATE FUNCTION app_private.oauth_client(p_client_id text)
  RETURNS TABLE(id uuid, client_name text, redirect_uris text[], fetched_at timestamptz)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.id, c.client_name, c.redirect_uris, c.fetched_at FROM public.oauth_clients c WHERE c.client_id = p_client_id
$$;

-- A pending request, for the consent page (replaces 0039's): also the client's ID, so the page can name
-- a metadata document's URL.
DROP FUNCTION app_private.oauth_pending(uuid);
CREATE FUNCTION app_private.oauth_pending(p_id uuid)
  RETURNS TABLE(client_name text, redirect_uri text, site text, client_id text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.client_name, a.redirect_uri, a.site, c.client_id FROM public.oauth_authorizations a
  JOIN public.oauth_clients c ON c.id = a.client
  WHERE a.id = p_id AND a.decided_at IS NULL AND a.expires_at > now()
$$;

-- ---------------------------------------------------------------- 2. MCP sessions
CREATE TABLE mcp_sessions (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  project_id uuid NOT NULL,
  session_hash bytea NOT NULL UNIQUE CHECK (octet_length(session_hash) = 32),
  user_id uuid REFERENCES users(id),  -- whose token opened it; NULL on a public site without one
  protocol_version text NOT NULL CHECK (protocol_version ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
  subscriptions text[] NOT NULL DEFAULT '{}'
    CHECK (cardinality(subscriptions) <= 50 AND char_length(array_to_string(subscriptions, '')) <= 50 * 2048),
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  CHECK (expires_at <= created_at + interval '7 days'),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id)
);
CREATE INDEX ix_mcp_sessions_expires ON mcp_sessions(expires_at);
CREATE INDEX ix_mcp_sessions_project ON mcp_sessions(workspace_id, project_id);
ALTER TABLE mcp_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE mcp_sessions FORCE ROW LEVEL SECURITY;
REVOKE ALL ON mcp_sessions FROM app_runtime;
CREATE POLICY tenant ON mcp_sessions TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
GRANT SELECT, INSERT, DELETE ON mcp_sessions TO app_runtime;
GRANT UPDATE (subscriptions, last_seen_at, expires_at) ON mcp_sessions TO app_runtime;

-- The hourly retention sweep ends sessions past their expiry, in every workspace.
CREATE POLICY resolver_sweep ON mcp_sessions FOR DELETE TO app_resolver USING (true);
CREATE POLICY resolver_sweep_read ON mcp_sessions FOR SELECT TO app_resolver USING (true);
GRANT SELECT (expires_at), DELETE ON mcp_sessions TO app_resolver;
CREATE FUNCTION app_private.sweep_mcp_sessions() RETURNS bigint
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n bigint;
BEGIN
  DELETE FROM public.mcp_sessions WHERE expires_at < now();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY['oauth_metadata_client(text, text, text[])', 'oauth_client(text)', 'oauth_pending(uuid)',
                           'sweep_mcp_sessions()'] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;
