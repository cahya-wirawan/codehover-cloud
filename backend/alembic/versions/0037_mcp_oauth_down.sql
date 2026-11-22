DROP FUNCTION app_private.sweep_oauth();
DROP FUNCTION app_private.oauth_revoke(bytea);
DROP FUNCTION app_private.oauth_refresh_grant(bytea, text);
DROP FUNCTION app_private.oauth_redeem(bytea, text, text);
DROP FUNCTION app_private.oauth_decide(uuid, uuid, uuid, boolean, bytea);
DROP FUNCTION app_private.oauth_pending(uuid);
DROP FUNCTION app_private.oauth_begin(uuid, text, text, text, text, text);
DROP FUNCTION app_private.oauth_client(text);
DROP FUNCTION app_private.oauth_register_client(text, text, text[]);
DROP FUNCTION app_private.access_token(bytea);
DELETE FROM access_tokens WHERE grant_id IS NOT NULL;
DROP POLICY resolver_revoke ON access_tokens;
REVOKE UPDATE (revoked_at) ON access_tokens FROM app_resolver;
REVOKE SELECT (grant_id, project_id) ON access_tokens FROM app_resolver;
DROP INDEX ix_access_tokens_grant;
ALTER TABLE access_tokens DROP CONSTRAINT access_tokens_prefix_check;
ALTER TABLE access_tokens ADD CONSTRAINT access_tokens_prefix_check CHECK (prefix ~ '^chm_[A-Za-z0-9_-]{4,12}$');
ALTER TABLE access_tokens DROP CONSTRAINT access_tokens_grant_project, DROP CONSTRAINT access_tokens_grant_fk;
ALTER TABLE access_tokens DROP COLUMN grant_id, DROP COLUMN project_id;
DROP TABLE oauth_grants;
DROP TABLE oauth_authorizations;
DROP TABLE oauth_clients;
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
