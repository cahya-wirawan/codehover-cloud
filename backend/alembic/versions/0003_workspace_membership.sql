-- 0003 workspace membership (backlog T04; 09 "Permission matrix").

-- Invitation acceptance happens before the invitee belongs to the workspace, so the token is resolved
-- by a lookup owned by app_resolver (no BYPASSRLS). The token itself never leaves the invitee.
CREATE POLICY resolver_select ON invitations FOR SELECT TO app_resolver USING (true);
GRANT SELECT (id, workspace_id, email_normalized, role, token_hash, expires_at, accepted_at, revoked_at)
  ON invitations TO app_resolver;

CREATE FUNCTION app_private.invitation_by_token(p_token_hash bytea)
  RETURNS TABLE(invitation_id uuid, workspace_id uuid, email_normalized text, role text,
                expires_at timestamptz, accepted_at timestamptz, revoked_at timestamptz)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.id, i.workspace_id, i.email_normalized, i.role, i.expires_at, i.accepted_at, i.revoked_at
  FROM public.invitations i WHERE i.token_hash = p_token_hash
$$;
ALTER FUNCTION app_private.invitation_by_token(bytea) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.invitation_by_token(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.invitation_by_token(bytea) TO app_runtime;

-- Owner invariant: every live workspace keeps at least one owner. Checked at commit (deferred), after
-- locking the workspace row: concurrent removals of the last two owners serialize, and the later one
-- re-counts and fails. Runs as the invoker, so RLS still applies. Workspaces being purged are exempt.
CREATE FUNCTION app_private.memberships_keep_owner() RETURNS trigger
  LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
  ws_status text;
  remaining integer;
BEGIN
  SELECT w.status INTO ws_status FROM public.workspaces w WHERE w.id = OLD.workspace_id FOR UPDATE;
  IF ws_status IS NULL OR ws_status = 'deleting' THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO remaining FROM public.memberships m
    WHERE m.workspace_id = OLD.workspace_id AND m.role = 'owner';
  IF remaining = 0 THEN
    RAISE EXCEPTION 'workspace would be left without an owner' USING ERRCODE = '23514',
      CONSTRAINT = 'memberships_keep_owner';
  END IF;
  RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION app_private.memberships_keep_owner() FROM PUBLIC;

CREATE CONSTRAINT TRIGGER memberships_keep_owner
  AFTER UPDATE OF role OR DELETE ON memberships
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW WHEN (OLD.role = 'owner')
  EXECUTE FUNCTION app_private.memberships_keep_owner();
