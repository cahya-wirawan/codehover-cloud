DROP FUNCTION app_private.sweep_handle_reservations();
DROP FUNCTION app_private.handle_unavailable(text, uuid);
DROP POLICY organization_coworker ON users;
DROP FUNCTION app_private.answer_organization_invitation(uuid, boolean);
DROP FUNCTION app_private.retire_namespace(uuid);
DROP FUNCTION app_private.rename_namespace(uuid, text);
DROP FUNCTION app_private.create_organization(text, text);
DROP FUNCTION app_private.create_workspace(text, text, uuid);
DROP FUNCTION app_private.create_user_with_identity(text, text, text, text, text, text);
DROP POLICY resolver_revoke ON oauth_grants;
REVOKE UPDATE (revoked_at), SELECT (workspace_id, user_id) ON oauth_grants FROM app_resolver;
REVOKE SELECT (workspace_id, user_id) ON access_tokens FROM app_resolver;
DROP POLICY resolver_org_sync ON memberships;
REVOKE UPDATE (role, via_organization, updated_at), DELETE ON memberships FROM app_resolver;
-- Without organizations, everyone keeps the role they have now as their own (an organization's
-- workspaces may have no other owner).
UPDATE memberships SET via_organization = false WHERE via_organization;
DROP TRIGGER organization_members_keep_owner ON organization_members;
DROP TRIGGER organization_members_sync ON organization_members;
DROP TRIGGER workspaces_org_access ON workspaces;
DROP TRIGGER memberships_direct_role ON memberships;
DROP FUNCTION app_private.organization_members_keep_owner();
DROP FUNCTION app_private.organization_members_sync();
DROP FUNCTION app_private.workspaces_org_access();
DROP FUNCTION app_private.sync_org_access(uuid, uuid);
DROP FUNCTION app_private.apply_org_role(uuid, uuid, text);
DROP FUNCTION app_private.memberships_direct_role();
DROP FUNCTION app_private.role_rank(text);
ALTER TABLE memberships DROP CONSTRAINT memberships_direct_role;
ALTER TABLE memberships DROP COLUMN via_organization, DROP COLUMN direct_role;
ALTER TABLE workspaces DROP CONSTRAINT workspaces_namespace;
ALTER TABLE workspaces DROP COLUMN namespace_id;
DROP TABLE handle_reservations;
DROP TABLE namespaces;
DROP POLICY member_read ON organizations;
DROP POLICY own_or_context ON organization_members;
DROP TABLE organization_invitations;
DROP TABLE organization_members;
DROP TABLE organizations;
DROP FUNCTION app_private.free_handle(text);
DROP FUNCTION app_private.valid_handle(text);
DROP FUNCTION app_private.handle_reserved(text);
CREATE FUNCTION app_private.create_workspace(ws_name text, ws_slug text) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid; wid uuid;
BEGIN
  IF uid IS NULL THEN RAISE EXCEPTION 'app.user_id not set' USING ERRCODE = '42501'; END IF;
  INSERT INTO public.workspaces(name, slug) VALUES (ws_name, ws_slug) RETURNING id INTO wid;
  INSERT INTO public.memberships(workspace_id, user_id, role) VALUES (wid, uid, 'owner');
  RETURN wid;
END $$;
CREATE FUNCTION app_private.create_user_with_identity(
  p_provider text, p_issuer text, p_subject text, p_display_name text, p_verified_email text
) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE uid uuid;
BEGIN
  INSERT INTO public.users(display_name, primary_email) VALUES (p_display_name, p_verified_email) RETURNING id INTO uid;
  INSERT INTO public.oauth_identities(user_id, provider, issuer, subject, verified_email)
    VALUES (uid, p_provider, p_issuer, p_subject, p_verified_email);
  RETURN uid;
END $$;
ALTER FUNCTION app_private.create_workspace(text, text) OWNER TO app_resolver;
ALTER FUNCTION app_private.create_user_with_identity(text, text, text, text, text) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.create_workspace(text, text),
  app_private.create_user_with_identity(text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.create_workspace(text, text),
  app_private.create_user_with_identity(text, text, text, text, text) TO app_runtime;
