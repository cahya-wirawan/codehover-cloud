-- 0035 personal access tokens for hosted MCP (06, 09).
--
-- A token belongs to one member of one workspace and only reads: it opens that workspace's private
-- published sites the member may see (app_private.project_role), through /mcp/{slug}. Only the SHA-256
-- digest of the token is stored; the first characters (`prefix`) identify it in lists. A token stops
-- working when it is revoked or expires, when its user is no longer active, and when the user leaves the
-- workspace (the resolver checks the membership on every request; removal also revokes the tokens).

CREATE TABLE access_tokens (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  user_id uuid NOT NULL REFERENCES users(id),
  name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
  token_hash bytea NOT NULL UNIQUE CHECK (length(token_hash) = 32),
  prefix text NOT NULL CHECK (prefix ~ '^chm_[A-Za-z0-9_-]{4,12}$'),
  scope text NOT NULL DEFAULT 'mcp:read' CHECK (scope IN ('mcp:read')),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  last_used_at timestamptz,
  revoked_at timestamptz,
  CHECK (expires_at > created_at AND expires_at <= created_at + interval '366 days'),
  UNIQUE (workspace_id, id)
);
CREATE INDEX ix_access_tokens_owner ON access_tokens(workspace_id, user_id, created_at DESC);

CREATE POLICY tenant ON access_tokens TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE access_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE access_tokens FORCE ROW LEVEL SECURITY;
REVOKE ALL ON access_tokens FROM app_runtime;
-- DELETE: the workspace purge. Only use and revocation change afterwards.
GRANT SELECT, INSERT, DELETE ON access_tokens TO app_runtime;
GRANT UPDATE (last_used_at, revoked_at) ON access_tokens TO app_runtime;

-- A request names only the token; its workspace and user come from this resolver (identifiers only).
CREATE POLICY resolver ON access_tokens FOR SELECT TO app_resolver USING (true);
GRANT SELECT (id, workspace_id, user_id, token_hash, scope, expires_at, revoked_at) ON access_tokens TO app_resolver;
CREATE FUNCTION app_private.access_token(p_hash bytea)
  RETURNS TABLE(token_id uuid, workspace_id uuid, user_id uuid, scope text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT t.id, t.workspace_id, t.user_id, t.scope FROM public.access_tokens t
  JOIN public.users u ON u.id = t.user_id AND u.status = 'active'
  JOIN public.workspaces w ON w.id = t.workspace_id AND w.status = 'active'
  WHERE t.token_hash = p_hash AND t.revoked_at IS NULL AND t.expires_at > now()
    AND EXISTS (SELECT 1 FROM public.memberships m WHERE m.workspace_id = t.workspace_id AND m.user_id = t.user_id)
$$;
ALTER FUNCTION app_private.access_token(bytea) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.access_token(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.access_token(bytea) TO app_runtime;
