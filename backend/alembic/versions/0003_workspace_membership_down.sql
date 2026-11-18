DROP TRIGGER memberships_keep_owner ON memberships;
DROP FUNCTION app_private.memberships_keep_owner();
DROP FUNCTION app_private.invitation_by_token(bytea);
REVOKE SELECT ON invitations FROM app_resolver;
DROP POLICY resolver_select ON invitations;
