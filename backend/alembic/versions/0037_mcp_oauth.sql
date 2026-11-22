-- 0037 MCP sign-in (OAuth 2.1 with PKCE) for private hosted MCP sites (06 "/oauth", 09).
--
-- An MCP client that meets a private site registers itself (RFC 7591, public clients only), sends its
-- person through the app's consent page, and trades the one-time code (PKCE S256, 60 s) for an access
-- token of one hour, bound to that site's project (RFC 8707), and a refresh token of 30 days that changes
-- on every use. A grant is one person's consent for one client and one project; revoking it, leaving the
-- workspace or losing access to the project ends every token it issued. Only digests of codes and tokens
-- are stored.
--
-- Clients and pending authorizations are global (no workspace until a person consents), so the runtime
-- reaches them only through the SECURITY DEFINER functions below (owned by app_resolver), as for sign-in's
-- oauth_transactions. Grants are tenant rows like access tokens.

CREATE TABLE oauth_clients (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  client_id text NOT NULL UNIQUE CHECK (client_id ~ '^chc_[A-Za-z0-9_-]{22,64}$'),
  client_name text NOT NULL CHECK (char_length(btrim(client_name)) BETWEEN 1 AND 100),
  redirect_uris text[] NOT NULL CHECK (cardinality(redirect_uris) BETWEEN 1 AND 5),
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);
ALTER TABLE oauth_clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_clients FORCE ROW LEVEL SECURITY;
REVOKE ALL ON oauth_clients FROM app_runtime;
CREATE POLICY resolver ON oauth_clients TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE (last_used_at), DELETE ON oauth_clients TO app_resolver;

CREATE TABLE oauth_authorizations (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  client uuid NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE,
  redirect_uri text NOT NULL CHECK (char_length(redirect_uri) <= 512),
  code_challenge text NOT NULL CHECK (code_challenge ~ '^[A-Za-z0-9_-]{43}$'),
  resource text NOT NULL CHECK (char_length(resource) <= 600),
  slug text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{2,62}$'),
  state text CHECK (char_length(state) <= 500),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now() + interval '10 minutes',
  decided_at timestamptz,
  approved boolean,
  user_id uuid REFERENCES users(id),
  workspace_id uuid,
  project_id uuid,
  code_hash bytea UNIQUE CHECK (length(code_hash) = 32),
  code_expires_at timestamptz,
  redeemed_at timestamptz,
  CHECK ((code_hash IS NULL) = (code_expires_at IS NULL)),
  CHECK (code_hash IS NULL OR (approved AND user_id IS NOT NULL AND workspace_id IS NOT NULL AND project_id IS NOT NULL))
);
CREATE INDEX ix_oauth_authorizations_created ON oauth_authorizations(created_at);
ALTER TABLE oauth_authorizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_authorizations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON oauth_authorizations FROM app_runtime;
CREATE POLICY resolver ON oauth_authorizations TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_authorizations TO app_resolver;

CREATE TABLE oauth_grants (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  user_id uuid NOT NULL REFERENCES users(id),
  project_id uuid NOT NULL,
  client uuid NOT NULL REFERENCES oauth_clients(id),
  client_name text NOT NULL CHECK (char_length(client_name) BETWEEN 1 AND 100),
  authorization_id uuid NOT NULL UNIQUE,
  refresh_hash bytea NOT NULL UNIQUE CHECK (length(refresh_hash) = 32),
  previous_refresh_hash bytea UNIQUE CHECK (length(previous_refresh_hash) = 32),
  refresh_expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  revoked_at timestamptz,
  CHECK (refresh_expires_at <= COALESCE(last_used_at, created_at) + interval '31 days'),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id)
);
CREATE INDEX ix_oauth_grants_owner ON oauth_grants(workspace_id, user_id, created_at DESC);
CREATE POLICY tenant ON oauth_grants TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE oauth_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_grants FORCE ROW LEVEL SECURITY;
REVOKE ALL ON oauth_grants FROM app_runtime;
-- DELETE: the workspace purge. Only rotation, use and revocation change a grant afterwards.
GRANT SELECT, INSERT, DELETE ON oauth_grants TO app_runtime;
GRANT UPDATE (refresh_hash, previous_refresh_hash, refresh_expires_at, last_used_at, revoked_at)
  ON oauth_grants TO app_runtime;
CREATE POLICY resolver ON oauth_grants TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT (id, workspace_id, user_id, project_id, client, authorization_id, refresh_hash,
              previous_refresh_hash, refresh_expires_at, revoked_at),
      UPDATE (revoked_at) ON oauth_grants TO app_resolver;

-- An access token issued through a grant names it and its project; a personal token has neither.
ALTER TABLE access_tokens ADD COLUMN grant_id uuid, ADD COLUMN project_id uuid;
ALTER TABLE access_tokens
  ADD CONSTRAINT access_tokens_grant_fk FOREIGN KEY (workspace_id, grant_id) REFERENCES oauth_grants(workspace_id, id),
  ADD CONSTRAINT access_tokens_grant_project CHECK ((grant_id IS NULL) = (project_id IS NULL));
ALTER TABLE access_tokens DROP CONSTRAINT access_tokens_prefix_check;
ALTER TABLE access_tokens ADD CONSTRAINT access_tokens_prefix_check
  CHECK (prefix ~ '^ch[mo]_[A-Za-z0-9_-]{4,12}$' AND (grant_id IS NULL) = (prefix LIKE 'chm\_%'));
CREATE INDEX ix_access_tokens_grant ON access_tokens(grant_id) WHERE grant_id IS NOT NULL;
GRANT SELECT (grant_id, project_id) ON access_tokens TO app_resolver;
GRANT UPDATE (revoked_at) ON access_tokens TO app_resolver;
CREATE POLICY resolver_revoke ON access_tokens FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);

-- The token resolver (0035) also returns the project an OAuth token is bound to, and refuses a token
-- whose grant was revoked.
DROP FUNCTION app_private.access_token(bytea);
CREATE FUNCTION app_private.access_token(p_hash bytea)
  RETURNS TABLE(token_id uuid, workspace_id uuid, user_id uuid, scope text, project_id uuid)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT t.id, t.workspace_id, t.user_id, t.scope, t.project_id FROM public.access_tokens t
  JOIN public.users u ON u.id = t.user_id AND u.status = 'active'
  JOIN public.workspaces w ON w.id = t.workspace_id AND w.status = 'active'
  WHERE t.token_hash = p_hash AND t.revoked_at IS NULL AND t.expires_at > now()
    AND EXISTS (SELECT 1 FROM public.memberships m WHERE m.workspace_id = t.workspace_id AND m.user_id = t.user_id)
    AND (t.grant_id IS NULL OR EXISTS (SELECT 1 FROM public.oauth_grants g
                                        WHERE g.id = t.grant_id AND g.revoked_at IS NULL))
$$;
ALTER FUNCTION app_private.access_token(bytea) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.access_token(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.access_token(bytea) TO app_runtime;

-- ---------------------------------------------------------------- functions
CREATE FUNCTION app_private.oauth_register_client(p_client_id text, p_name text, p_uris text[])
  RETURNS timestamptz LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  INSERT INTO public.oauth_clients(client_id, client_name, redirect_uris) VALUES (p_client_id, p_name, p_uris)
  RETURNING created_at
$$;

CREATE FUNCTION app_private.oauth_client(p_client_id text)
  RETURNS TABLE(id uuid, client_name text, redirect_uris text[])
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.id, c.client_name, c.redirect_uris FROM public.oauth_clients c WHERE c.client_id = p_client_id
$$;

CREATE FUNCTION app_private.oauth_begin(p_client uuid, p_redirect_uri text, p_challenge text, p_resource text,
                                        p_slug text, p_state text)
  RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  INSERT INTO public.oauth_authorizations(client, redirect_uri, code_challenge, resource, slug, state)
  VALUES (p_client, p_redirect_uri, p_challenge, p_resource, p_slug, p_state) RETURNING id
$$;

-- A pending request, for the consent page: what the client asks for, nothing about the person.
CREATE FUNCTION app_private.oauth_pending(p_id uuid)
  RETURNS TABLE(client_name text, redirect_uri text, slug text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.client_name, a.redirect_uri, a.slug FROM public.oauth_authorizations a
  JOIN public.oauth_clients c ON c.id = a.client
  WHERE a.id = p_id AND a.decided_at IS NULL AND a.expires_at > now()
$$;

-- The signed-in person (app.user_id) decides once. Approval needs their access to the project, checked
-- again here; the code's digest comes from the caller, the code itself never reaches the database.
CREATE FUNCTION app_private.oauth_decide(p_id uuid, p_workspace uuid, p_project uuid, p_approve boolean,
                                         p_code_hash bytea)
  RETURNS TABLE(redirect_uri text, state text)
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  person uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
BEGIN
  IF person IS NULL THEN
    RAISE EXCEPTION 'a signed-in person decides' USING ERRCODE = '42501';
  END IF;
  IF p_approve AND app_private.project_role(p_workspace, p_project, person) IS NULL THEN
    RAISE EXCEPTION 'no access to this project' USING ERRCODE = '42501';
  END IF;
  RETURN QUERY
  UPDATE public.oauth_authorizations a SET decided_at = now(), approved = p_approve,
         user_id = CASE WHEN p_approve THEN person END,
         workspace_id = CASE WHEN p_approve THEN p_workspace END,
         project_id = CASE WHEN p_approve THEN p_project END,
         code_hash = CASE WHEN p_approve THEN p_code_hash END,
         code_expires_at = CASE WHEN p_approve THEN now() + interval '60 seconds' END
   WHERE a.id = p_id AND a.decided_at IS NULL AND a.expires_at > now()
  RETURNING a.redirect_uri, a.state;
END $$;

-- Trade a code once. A second use of a code (an intercepted code replayed) ends the grant made from it.
CREATE FUNCTION app_private.oauth_redeem(p_code_hash bytea, p_client_id text, p_redirect_uri text)
  RETURNS TABLE(authorization_id uuid, client uuid, client_name text, workspace_id uuid, user_id uuid,
                project_id uuid, code_challenge text, resource text)
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  a public.oauth_authorizations;
BEGIN
  SELECT * INTO a FROM public.oauth_authorizations x WHERE x.code_hash = p_code_hash FOR UPDATE;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF a.redeemed_at IS NOT NULL THEN
    UPDATE public.oauth_grants g SET revoked_at = now() WHERE g.authorization_id = a.id AND g.revoked_at IS NULL;
    RETURN;
  END IF;
  UPDATE public.oauth_authorizations x SET redeemed_at = now() WHERE x.id = a.id;
  IF a.code_expires_at <= now() OR a.redirect_uri <> p_redirect_uri
     OR NOT EXISTS (SELECT 1 FROM public.oauth_clients c WHERE c.id = a.client AND c.client_id = p_client_id) THEN
    RETURN;
  END IF;
  UPDATE public.oauth_clients c SET last_used_at = now() WHERE c.id = a.client;
  RETURN QUERY SELECT a.id, a.client, c.client_name, a.workspace_id, a.user_id, a.project_id,
                      a.code_challenge, a.resource
                 FROM public.oauth_clients c WHERE c.id = a.client;
END $$;

-- The grant a refresh token belongs to. A refresh token that was already rotated away (replayed) ends
-- the grant, as RFC 9700 advises for public clients.
CREATE FUNCTION app_private.oauth_refresh_grant(p_hash bytea, p_client_id text)
  RETURNS TABLE(grant_id uuid, workspace_id uuid, user_id uuid, project_id uuid)
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  g record;
BEGIN
  SELECT x.id, x.workspace_id, x.user_id, x.project_id, x.previous_refresh_hash, x.refresh_expires_at,
         x.revoked_at INTO g
    FROM public.oauth_grants x
   WHERE (x.refresh_hash = p_hash OR x.previous_refresh_hash = p_hash)
     AND EXISTS (SELECT 1 FROM public.oauth_clients c WHERE c.id = x.client AND c.client_id = p_client_id);
  IF NOT FOUND OR g.revoked_at IS NOT NULL THEN
    RETURN;
  END IF;
  IF g.previous_refresh_hash = p_hash THEN
    UPDATE public.oauth_grants x SET revoked_at = now() WHERE x.id = g.id;
    RETURN;
  END IF;
  IF g.refresh_expires_at <= now() THEN
    RETURN;
  END IF;
  RETURN QUERY SELECT g.id, g.workspace_id, g.user_id, g.project_id;
END $$;

-- RFC 7009: whoever holds a token may end it. An OAuth token (access or refresh) ends its whole grant.
CREATE FUNCTION app_private.oauth_revoke(p_hash bytea) RETURNS void
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  UPDATE public.oauth_grants g SET revoked_at = now()
   WHERE g.revoked_at IS NULL AND (g.refresh_hash = p_hash OR g.id IN (
     SELECT t.grant_id FROM public.access_tokens t WHERE t.token_hash = p_hash AND t.grant_id IS NOT NULL));
  UPDATE public.access_tokens t SET revoked_at = now() WHERE t.token_hash = p_hash AND t.revoked_at IS NULL;
END $$;

-- The hourly retention sweep: authorizations after a day (their codes lasted 60 s), clients nobody used within
-- 30 days of registering and that hold no grant.
CREATE FUNCTION app_private.sweep_oauth() RETURNS bigint
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  a bigint; c bigint;
BEGIN
  DELETE FROM public.oauth_authorizations x WHERE x.created_at < now() - interval '1 day';
  GET DIAGNOSTICS a = ROW_COUNT;
  DELETE FROM public.oauth_clients c WHERE COALESCE(c.last_used_at, c.created_at) < now() - interval '30 days'
    AND NOT EXISTS (SELECT 1 FROM public.oauth_grants g WHERE g.client = c.id)
    AND NOT EXISTS (SELECT 1 FROM public.oauth_authorizations x WHERE x.client = c.id);
  GET DIAGNOSTICS c = ROW_COUNT;
  RETURN a + c;
END $$;

DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY[
    'oauth_register_client(text, text, text[])', 'oauth_client(text)',
    'oauth_begin(uuid, text, text, text, text, text)', 'oauth_pending(uuid)',
    'oauth_decide(uuid, uuid, uuid, boolean, bytea)', 'oauth_redeem(bytea, text, text)',
    'oauth_refresh_grant(bytea, text)', 'oauth_revoke(bytea)', 'sweep_oauth()'] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;
