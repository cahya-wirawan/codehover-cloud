-- Back to 0042: stateless MCP, registered clients only (metadata-document clients and their grants end).
DROP FUNCTION app_private.sweep_mcp_sessions();
DROP TABLE mcp_sessions;

DROP FUNCTION app_private.oauth_pending(uuid);
CREATE FUNCTION app_private.oauth_pending(p_id uuid)
  RETURNS TABLE(client_name text, redirect_uri text, site text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.client_name, a.redirect_uri, a.site FROM public.oauth_authorizations a
  JOIN public.oauth_clients c ON c.id = a.client
  WHERE a.id = p_id AND a.decided_at IS NULL AND a.expires_at > now()
$$;
DROP FUNCTION app_private.oauth_client(text);
CREATE FUNCTION app_private.oauth_client(p_client_id text)
  RETURNS TABLE(id uuid, client_name text, redirect_uris text[])
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.id, c.client_name, c.redirect_uris FROM public.oauth_clients c WHERE c.client_id = p_client_id
$$;
DROP FUNCTION app_private.oauth_metadata_client(text, text, text[]);
DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY['oauth_client(text)', 'oauth_pending(uuid)'] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;

DELETE FROM access_tokens WHERE grant_id IN (
  SELECT g.id FROM oauth_grants g JOIN oauth_clients c ON c.id = g.client WHERE c.client_id LIKE 'https://%');
DELETE FROM oauth_grants WHERE client IN (SELECT id FROM oauth_clients WHERE client_id LIKE 'https://%');
DELETE FROM oauth_clients WHERE client_id LIKE 'https://%';
REVOKE UPDATE (client_name, redirect_uris, fetched_at) ON oauth_clients FROM app_resolver;
ALTER TABLE oauth_clients DROP CONSTRAINT oauth_clients_fetched;
ALTER TABLE oauth_clients DROP COLUMN fetched_at;
ALTER TABLE oauth_clients DROP CONSTRAINT oauth_clients_client_id_check;
ALTER TABLE oauth_clients ADD CONSTRAINT oauth_clients_client_id_check CHECK (client_id ~ '^chc_[A-Za-z0-9_-]{22,64}$');
