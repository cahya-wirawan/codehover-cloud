-- 0038 namespaces, user handles and organizations (01, 03, 05, 06, 09).
--
-- GitHub-like owners: every person and every organization has one handle in a single global namespace
-- (`namespaces`). Workspaces now belong to a namespace: a person's personal one or an organization's.
-- An organization is a level above workspaces with its own members (owner, admin, member); its owners
-- and admins are owners and admins of every workspace it holds, and its plain members see only the
-- workspaces they are added to.
--
-- Organization access is materialized as membership rows (`memberships.via_organization`), so every
-- existing check (workspace roles, project access, tokens, RLS) keeps working unchanged. A row keeps the
-- person's own workspace role in `direct_role`; when their organization role ends, the row falls back to
-- it, or is removed (with the person's tokens and MCP sign-ins there) when they had none.
--
-- A handle that is let go (a rename, a deleted account or organization) stays reserved for its namespace
-- for 90 days, three per namespace, like site addresses (0036).

-- ---------------------------------------------------------------- handles
CREATE FUNCTION app_private.handle_reserved(p_handle text) RETURNS boolean
  LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
  SELECT p_handle = ANY (ARRAY[
    'about', 'account', 'accounts', 'admin', 'deleted', 'api', 'app', 'assets', 'auth', 'billing', 'codehover',
    'docs', 'help', 'invitations', 'login', 'logout', 'me', 'mcp', 'new', 'o', 'oauth', 'ops', 'org',
    'orgs', 'organization', 'organizations', 'p', 'privacy', 'published', 'root', 'settings', 'signin',
    'signup', 'site', 'sites', 'static', 'support', 'system', 'terms', 'w', 'welcome', 'workspace',
    'workspaces', 'www'])
$$;

CREATE FUNCTION app_private.valid_handle(p_handle text) RETURNS boolean
  LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
  SELECT p_handle IS NOT NULL AND char_length(p_handle) BETWEEN 1 AND 39
     AND p_handle ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND NOT app_private.handle_reserved(p_handle)
     AND p_handle !~ '^deleted-'  -- the shape of a deleted owner's tombstone, never chosen
$$;

-- A free handle from a hint (the GitHub login, which is public already), or a random one: never from an
-- email address or a name, since a handle is public (security review).
CREATE FUNCTION app_private.free_handle(p_hint text) RETURNS text
  LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, public AS $$
DECLARE
  base text := left(btrim(regexp_replace(lower(COALESCE(p_hint, '')), '[^a-z0-9]+', '-', 'g'), '-'), 30);
  candidate text;
  n integer := 1;
BEGIN
  base := btrim(base, '-');
  IF NOT app_private.valid_handle(base) THEN
    base := 'user-' || substr(md5(random()::text || clock_timestamp()::text), 1, 8);
  END IF;
  candidate := base;
  WHILE EXISTS (SELECT 1 FROM public.namespaces x WHERE x.handle = candidate)
     OR EXISTS (SELECT 1 FROM public.handle_reservations r WHERE r.handle = candidate AND r.reserved_until > now())
     OR NOT app_private.valid_handle(candidate) LOOP
    n := n + 1;
    candidate := base || '-' || n;
  END LOOP;
  RETURN candidate;
END $$;

-- ---------------------------------------------------------------- organizations
CREATE TABLE organizations (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  name varchar(80) NOT NULL CHECK (char_length(btrim(name)) >= 2),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleting')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE TABLE organization_members (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  organization_id uuid NOT NULL REFERENCES organizations(id),
  user_id uuid NOT NULL REFERENCES users(id),
  role text NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, user_id)
);
CREATE INDEX ix_organization_members_user ON organization_members(user_id);

-- An invitation names a person by their handle; they accept or decline it in the app.
CREATE TABLE organization_invitations (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  organization_id uuid NOT NULL REFERENCES organizations(id),
  user_id uuid NOT NULL REFERENCES users(id),
  role text NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
  invited_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now() + interval '14 days',
  accepted_at timestamptz,
  declined_at timestamptz,
  revoked_at timestamptz
);
CREATE UNIQUE INDEX uq_organization_invitations_open ON organization_invitations(organization_id, user_id)
  WHERE accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL;
CREATE INDEX ix_organization_invitations_user ON organization_invitations(user_id);

-- ---------------------------------------------------------------- namespaces
CREATE TABLE namespaces (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  handle text NOT NULL UNIQUE CHECK (app_private.valid_handle(handle) OR handle ~ '^deleted-[0-9a-f]{32}$'),
  kind text NOT NULL CHECK (kind IN ('user', 'organization')),
  user_id uuid UNIQUE REFERENCES users(id),
  organization_id uuid UNIQUE REFERENCES organizations(id),
  confirmed_at timestamptz,  -- a person confirms (or changes) the handle suggested at sign-up
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'user') = (user_id IS NOT NULL)),
  CHECK ((kind = 'organization') = (organization_id IS NOT NULL))
);

CREATE TABLE handle_reservations (
  handle text PRIMARY KEY,
  namespace_id uuid REFERENCES namespaces(id),  -- NULL: a deleted owner's handle, reserved for nobody
  released_at timestamptz NOT NULL,
  reserved_until timestamptz NOT NULL,
  CHECK (reserved_until > released_at)
);
CREATE INDEX ix_handle_reservations_namespace ON handle_reservations(namespace_id, released_at DESC);

-- Every person gets a personal namespace with a random suggested handle (new accounts: their GitHub
-- login when they sign in with GitHub), to confirm or change in the app.
DO $$
DECLARE u record;
BEGIN
  FOR u IN SELECT id, display_name, primary_email, status FROM public.users ORDER BY created_at, id LOOP
    INSERT INTO public.namespaces(handle, kind, user_id)
    VALUES (CASE WHEN u.status = 'deleted' THEN 'deleted-' || replace(u.id::text, '-', '')
                 ELSE app_private.free_handle(NULL) END,
            'user', u.id);
  END LOOP;
END $$;

-- ---------------------------------------------------------------- workspaces belong to a namespace
ALTER TABLE workspaces ADD COLUMN namespace_id uuid REFERENCES namespaces(id);
UPDATE workspaces w SET namespace_id = n.id
  FROM namespaces n
 WHERE n.user_id = COALESCE(
   (SELECT m.user_id FROM memberships m WHERE m.workspace_id = w.id AND m.role = 'owner' ORDER BY m.created_at, m.id LIMIT 1),
   (SELECT m.user_id FROM memberships m WHERE m.workspace_id = w.id ORDER BY m.created_at, m.id LIMIT 1));
-- A purged workspace has no members left (and needs no owner); every other one has its namespace.
UPDATE workspaces w SET namespace_id = n.id
  FROM namespaces n
 WHERE w.namespace_id IS NULL AND n.user_id = (
   SELECT a.actor_user_id FROM audit_events a
    WHERE a.workspace_id = w.id AND a.action = 'workspace.created' ORDER BY a.created_at LIMIT 1);
-- A live workspace nobody belongs to (none should exist: every workspace keeps an owner) gets an
-- organization of its own, so no live workspace is left without an owner namespace.
DO $$
DECLARE w record; oid uuid;
BEGIN
  FOR w IN SELECT x.id, x.name, x.slug FROM public.workspaces x WHERE x.namespace_id IS NULL AND x.deleted_at IS NULL LOOP
    INSERT INTO public.organizations(name) VALUES (w.name) RETURNING id INTO oid;
    INSERT INTO public.namespaces(handle, kind, organization_id, confirmed_at)
    VALUES (app_private.free_handle(w.slug), 'organization', oid, now());
    UPDATE public.workspaces x SET namespace_id = (SELECT n.id FROM public.namespaces n WHERE n.organization_id = oid)
     WHERE x.id = w.id;
  END LOOP;
END $$;
ALTER TABLE workspaces ADD CONSTRAINT workspaces_namespace CHECK (namespace_id IS NOT NULL OR deleted_at IS NOT NULL);
CREATE INDEX ix_workspaces_namespace ON workspaces(namespace_id);

-- ---------------------------------------------------------------- memberships from an organization
ALTER TABLE memberships ADD COLUMN via_organization boolean NOT NULL DEFAULT false,
                        ADD COLUMN direct_role text CHECK (direct_role IN ('owner', 'admin', 'editor', 'viewer'));
UPDATE memberships SET direct_role = role;
ALTER TABLE memberships ADD CONSTRAINT memberships_direct_role CHECK (via_organization OR direct_role = role);

-- A change made directly (not by the organization sync) is the person's own role.
CREATE FUNCTION app_private.memberships_direct_role() RETURNS trigger
  LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF NOT NEW.via_organization THEN
    NEW.direct_role := NEW.role;
  END IF;
  RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION app_private.memberships_direct_role() FROM PUBLIC;
CREATE TRIGGER memberships_direct_role BEFORE INSERT OR UPDATE ON memberships
  FOR EACH ROW EXECUTE FUNCTION app_private.memberships_direct_role();

CREATE FUNCTION app_private.role_rank(p_role text) RETURNS integer
  LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
  SELECT CASE p_role WHEN 'owner' THEN 4 WHEN 'admin' THEN 3 WHEN 'editor' THEN 2 WHEN 'viewer' THEN 1 ELSE 0 END
$$;

-- What one person's organization role gives them in one workspace of the organization.
CREATE FUNCTION app_private.apply_org_role(p_workspace uuid, p_user uuid, p_grant text) RETURNS void
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  m record;
BEGIN
  SELECT x.id, x.role, x.direct_role, x.via_organization INTO m FROM public.memberships x
   WHERE x.workspace_id = p_workspace AND x.user_id = p_user FOR UPDATE;
  IF p_grant IS NULL THEN
    IF NOT FOUND OR NOT m.via_organization THEN
      RETURN;
    END IF;
    IF m.direct_role IS NULL THEN
      DELETE FROM public.memberships x WHERE x.id = m.id;
      UPDATE public.access_tokens t SET revoked_at = now()
       WHERE t.workspace_id = p_workspace AND t.user_id = p_user AND t.revoked_at IS NULL;
      UPDATE public.oauth_grants g SET revoked_at = now()
       WHERE g.workspace_id = p_workspace AND g.user_id = p_user AND g.revoked_at IS NULL;
    ELSE
      UPDATE public.memberships x SET role = m.direct_role, via_organization = false, updated_at = now()
       WHERE x.id = m.id;
    END IF;
    RETURN;
  END IF;
  IF NOT FOUND THEN
    INSERT INTO public.memberships(workspace_id, user_id, role, via_organization, direct_role)
    VALUES (p_workspace, p_user, p_grant, true, NULL);
  ELSE
    UPDATE public.memberships x
       SET role = CASE WHEN app_private.role_rank(m.direct_role) > app_private.role_rank(p_grant)
                       THEN m.direct_role ELSE p_grant END,
           via_organization = true, updated_at = now()
     WHERE x.id = m.id;
  END IF;
END $$;

CREATE FUNCTION app_private.sync_org_access(p_org uuid, p_user uuid) RETURNS void
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  granted text;
  w record;
BEGIN
  SELECT CASE om.role WHEN 'owner' THEN 'owner' WHEN 'admin' THEN 'admin' END INTO granted
    FROM public.organization_members om WHERE om.organization_id = p_org AND om.user_id = p_user;
  FOR w IN SELECT ws.id FROM public.workspaces ws JOIN public.namespaces n ON n.id = ws.namespace_id
            WHERE n.organization_id = p_org AND ws.deleted_at IS NULL ORDER BY ws.id LOOP
    PERFORM app_private.apply_org_role(w.id, p_user, granted);
  END LOOP;
END $$;

CREATE FUNCTION app_private.organization_members_sync() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    PERFORM app_private.sync_org_access(OLD.organization_id, OLD.user_id);
  END IF;
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    PERFORM app_private.sync_org_access(NEW.organization_id, NEW.user_id);
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER organization_members_sync AFTER INSERT OR UPDATE OF role OR DELETE ON organization_members
  FOR EACH ROW EXECUTE FUNCTION app_private.organization_members_sync();

-- A new workspace of an organization gets its owners and admins at once.
CREATE FUNCTION app_private.workspaces_org_access() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  om record;
BEGIN
  FOR om IN SELECT x.user_id, x.role FROM public.organization_members x
              JOIN public.namespaces n ON n.organization_id = x.organization_id
             WHERE n.id = NEW.namespace_id AND x.role IN ('owner', 'admin') LOOP
    PERFORM app_private.apply_org_role(NEW.id, om.user_id, om.role);
  END LOOP;
  RETURN NULL;
END $$;
CREATE TRIGGER workspaces_org_access AFTER INSERT ON workspaces
  FOR EACH ROW EXECUTE FUNCTION app_private.workspaces_org_access();

-- Owner invariant for organizations, as for workspaces (0003): checked at commit, after locking the row.
CREATE FUNCTION app_private.organization_members_keep_owner() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  PERFORM 1 FROM public.organizations o WHERE o.id = OLD.organization_id AND o.status <> 'deleting' FOR UPDATE;
  IF FOUND AND NOT EXISTS (SELECT 1 FROM public.organization_members m
                            WHERE m.organization_id = OLD.organization_id AND m.role = 'owner') THEN
    RAISE EXCEPTION 'organization would be left without an owner' USING ERRCODE = '23514',
      CONSTRAINT = 'organization_members_keep_owner';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER organization_members_keep_owner
  AFTER UPDATE OF role OR DELETE ON organization_members
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW WHEN (OLD.role = 'owner')
  EXECUTE FUNCTION app_private.organization_members_keep_owner();

-- ---------------------------------------------------------------- functions for the runtime
-- First sign-in (replaces 0002's): the person, their identity and their personal namespace, whose handle
-- is suggested from `p_handle_hint` and confirmed later in the app.
DROP FUNCTION app_private.create_user_with_identity(text, text, text, text, text);
CREATE FUNCTION app_private.create_user_with_identity(
  p_provider text, p_issuer text, p_subject text, p_display_name text, p_verified_email text, p_handle_hint text
) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE uid uuid;
BEGIN
  INSERT INTO public.users(display_name, primary_email) VALUES (p_display_name, p_verified_email) RETURNING id INTO uid;
  INSERT INTO public.oauth_identities(user_id, provider, issuer, subject, verified_email)
    VALUES (uid, p_provider, p_issuer, p_subject, p_verified_email);
  PERFORM pg_advisory_xact_lock(hashtextextended('namespace-handles', 0));
  INSERT INTO public.namespaces(handle, kind, user_id)
    VALUES (app_private.free_handle(p_handle_hint), 'user', uid);
  RETURN uid;
END $$;

-- New workspaces (replaces 0002's): in the caller's personal namespace, or in an organization the caller
-- owns or administers. In an organization the owners and admins come from it (workspaces_org_access);
-- in a personal namespace the caller is the owner, as before.
DROP FUNCTION app_private.create_workspace(text, text);
CREATE FUNCTION app_private.create_workspace(ws_name text, ws_slug text, p_organization uuid DEFAULT NULL)
  RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid; wid uuid; ns uuid;
BEGIN
  IF uid IS NULL THEN RAISE EXCEPTION 'app.user_id not set' USING ERRCODE = '42501'; END IF;
  IF p_organization IS NULL THEN
    SELECT n.id INTO ns FROM public.namespaces n WHERE n.user_id = uid;
  ELSE
    SELECT n.id INTO ns FROM public.namespaces n
      JOIN public.organizations o ON o.id = n.organization_id AND o.status = 'active'
      JOIN public.organization_members m ON m.organization_id = o.id AND m.user_id = uid AND m.role IN ('owner', 'admin')
     WHERE n.organization_id = p_organization;
    IF ns IS NULL THEN RAISE EXCEPTION 'not an owner or admin of this organization' USING ERRCODE = '42501'; END IF;
  END IF;
  INSERT INTO public.workspaces(name, slug, namespace_id) VALUES (ws_name, ws_slug, ns) RETURNING id INTO wid;
  IF p_organization IS NULL THEN
    INSERT INTO public.memberships(workspace_id, user_id, role) VALUES (wid, uid, 'owner');
  END IF;
  RETURN wid;
END $$;

CREATE FUNCTION app_private.create_organization(p_name text, p_handle text) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid; oid uuid;
BEGIN
  IF uid IS NULL THEN RAISE EXCEPTION 'app.user_id not set' USING ERRCODE = '42501'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('namespace-handles', 0));
  IF EXISTS (SELECT 1 FROM public.handle_reservations r WHERE r.handle = p_handle AND r.reserved_until > now()) THEN
    RAISE EXCEPTION 'this handle was released recently and is reserved' USING ERRCODE = '23505',
      CONSTRAINT = 'namespaces_handle_reserved';
  END IF;
  INSERT INTO public.organizations(name) VALUES (p_name) RETURNING id INTO oid;
  INSERT INTO public.namespaces(handle, kind, organization_id, confirmed_at) VALUES (p_handle, 'organization', oid, now());
  INSERT INTO public.organization_members(organization_id, user_id, role) VALUES (oid, uid, 'owner');
  RETURN oid;
END $$;

-- Rename (or confirm) a namespace's handle. A person renames their own; an organization's owners rename
-- its. The old handle stays reserved for this namespace for 90 days (three at a time); a handle another
-- namespace let go recently cannot be taken.
CREATE FUNCTION app_private.rename_namespace(p_namespace uuid, p_handle text) RETURNS text
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
  n record;
BEGIN
  SELECT x.id, x.handle, x.kind, x.user_id, x.organization_id INTO n FROM public.namespaces x
   WHERE x.id = p_namespace FOR UPDATE;
  IF NOT FOUND OR uid IS NULL OR NOT (
       (n.kind = 'user' AND n.user_id = uid)
    OR (n.kind = 'organization' AND EXISTS (SELECT 1 FROM public.organization_members m
          WHERE m.organization_id = n.organization_id AND m.user_id = uid AND m.role = 'owner'))) THEN
    RAISE EXCEPTION 'not allowed to rename this namespace' USING ERRCODE = '42501';
  END IF;
  IF n.handle = p_handle THEN
    UPDATE public.namespaces x SET confirmed_at = COALESCE(x.confirmed_at, now()), updated_at = now() WHERE x.id = n.id;
    RETURN n.handle;
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('namespace-handles', 0));
  IF EXISTS (SELECT 1 FROM public.handle_reservations r WHERE r.handle = p_handle AND r.reserved_until > now()
               AND r.namespace_id IS DISTINCT FROM n.id) THEN
    RAISE EXCEPTION 'this handle was released recently and is reserved' USING ERRCODE = '23505',
      CONSTRAINT = 'namespaces_handle_reserved';
  END IF;
  DELETE FROM public.handle_reservations r WHERE r.handle = p_handle;
  UPDATE public.namespaces x SET handle = p_handle, confirmed_at = now(), updated_at = now() WHERE x.id = n.id;
  INSERT INTO public.handle_reservations(handle, namespace_id, released_at, reserved_until)
  VALUES (n.handle, n.id, clock_timestamp(), clock_timestamp() + interval '90 days')
  ON CONFLICT (handle) DO UPDATE SET namespace_id = EXCLUDED.namespace_id, released_at = EXCLUDED.released_at,
                                     reserved_until = EXCLUDED.reserved_until;
  DELETE FROM public.handle_reservations r WHERE r.namespace_id = n.id
     AND r.handle IN (SELECT y.handle FROM public.handle_reservations y WHERE y.namespace_id = n.id
                       ORDER BY y.released_at DESC, y.handle OFFSET 3);
  RETURN p_handle;
END $$;

-- A deleted account or organization lets its handle go: reserved for nobody for 90 days.
CREATE FUNCTION app_private.retire_namespace(p_namespace uuid) RETURNS void
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
  n record;
  old text;
BEGIN
  SELECT x.handle, x.kind, x.user_id, x.organization_id INTO n FROM public.namespaces x WHERE x.id = p_namespace FOR UPDATE;
  IF NOT FOUND OR n.handle LIKE 'deleted-%' THEN
    RETURN;
  END IF;
  -- Only the person themselves, or an owner of the organization, lets a handle go.
  IF uid IS NULL OR NOT ((n.kind = 'user' AND n.user_id = uid)
     OR (n.kind = 'organization' AND EXISTS (SELECT 1 FROM public.organization_members m
           WHERE m.organization_id = n.organization_id AND m.user_id = uid AND m.role = 'owner'))) THEN
    RAISE EXCEPTION 'not allowed to retire this namespace' USING ERRCODE = '42501';
  END IF;
  old := n.handle;
  UPDATE public.namespaces x SET handle = 'deleted-' || replace(x.id::text, '-', ''), updated_at = now()
   WHERE x.id = p_namespace;
  INSERT INTO public.handle_reservations(handle, namespace_id, released_at, reserved_until)
  VALUES (old, NULL, clock_timestamp(), clock_timestamp() + interval '90 days')
  ON CONFLICT (handle) DO UPDATE SET namespace_id = NULL, released_at = EXCLUDED.released_at,
                                     reserved_until = EXCLUDED.reserved_until;
END $$;

-- Accept or decline one's own invitation (the invitee from app.user_id).
CREATE FUNCTION app_private.answer_organization_invitation(p_invitation uuid, p_accept boolean) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
  i record;
BEGIN
  SELECT x.id, x.organization_id, x.role INTO i FROM public.organization_invitations x
    JOIN public.organizations o ON o.id = x.organization_id AND o.status = 'active'
   WHERE x.id = p_invitation AND x.user_id = uid AND x.accepted_at IS NULL AND x.declined_at IS NULL
     AND x.revoked_at IS NULL AND x.expires_at > now()
   FOR UPDATE OF x;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  IF p_accept THEN
    UPDATE public.organization_invitations x SET accepted_at = now() WHERE x.id = i.id;
    INSERT INTO public.organization_members(organization_id, user_id, role) VALUES (i.organization_id, uid, i.role)
    ON CONFLICT (organization_id, user_id) DO UPDATE
      SET role = CASE WHEN app_private.role_rank(EXCLUDED.role) > app_private.role_rank(public.organization_members.role)
                      THEN EXCLUDED.role ELSE public.organization_members.role END, updated_at = now();
  ELSE
    UPDATE public.organization_invitations x SET declined_at = now() WHERE x.id = i.id;
  END IF;
  RETURN i.organization_id;
END $$;

-- Whether a namespace may take a handle: NULL if it may, else 'invalid', 'taken' or 'reserved'.
CREATE FUNCTION app_private.handle_unavailable(p_handle text, p_namespace uuid) RETURNS text
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT CASE
    WHEN NOT app_private.valid_handle(p_handle) THEN 'invalid'
    WHEN EXISTS (SELECT 1 FROM public.namespaces n WHERE n.handle = p_handle AND n.id IS DISTINCT FROM p_namespace) THEN 'taken'
    WHEN EXISTS (SELECT 1 FROM public.handle_reservations r WHERE r.handle = p_handle AND r.reserved_until > now()
                   AND r.namespace_id IS DISTINCT FROM p_namespace) THEN 'reserved'
  END
$$;

-- The hourly retention sweep removes ended handle reservations.
CREATE FUNCTION app_private.sweep_handle_reservations() RETURNS bigint
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n bigint;
BEGIN
  DELETE FROM public.handle_reservations WHERE reserved_until < now();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- ---------------------------------------------------------------- privileges and policies
-- Handles are public (they appear in site addresses): the runtime reads namespaces, never writes them.
ALTER TABLE namespaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE namespaces FORCE ROW LEVEL SECURITY;
REVOKE ALL ON namespaces FROM app_runtime;
GRANT SELECT ON namespaces TO app_runtime;
CREATE POLICY public_read ON namespaces FOR SELECT TO app_runtime USING (true);
CREATE POLICY resolver ON namespaces TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE (handle, confirmed_at, updated_at) ON namespaces TO app_resolver;

ALTER TABLE handle_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE handle_reservations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON handle_reservations FROM app_runtime;
CREATE POLICY resolver ON handle_reservations TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON handle_reservations TO app_resolver;

-- Organizations: their members see them; changes happen in the organization's context (app.organization_id),
-- which the service sets only after checking the caller's organization role.
ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE organizations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON organizations FROM app_runtime;
GRANT SELECT, UPDATE (name, status, updated_at, deleted_at) ON organizations TO app_runtime;
CREATE POLICY member_read ON organizations FOR SELECT TO app_runtime
  USING (id = NULLIF(current_setting('app.organization_id', true), '')::uuid
         OR id IN (SELECT m.organization_id FROM organization_members m
                   WHERE m.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
         OR id IN (SELECT i.organization_id FROM organization_invitations i
                   WHERE i.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid));
CREATE POLICY context_update ON organizations FOR UPDATE TO app_runtime
  USING (id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
CREATE POLICY resolver ON organizations TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE (updated_at) ON organizations TO app_resolver;  -- UPDATE: the owner check locks the row

ALTER TABLE organization_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_members FORCE ROW LEVEL SECURITY;
REVOKE ALL ON organization_members FROM app_runtime;
GRANT SELECT, DELETE, UPDATE (role, updated_at) ON organization_members TO app_runtime;
CREATE POLICY own_or_context ON organization_members FOR SELECT TO app_runtime
  USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
         OR organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
CREATE POLICY context_write ON organization_members FOR UPDATE TO app_runtime
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
CREATE POLICY context_delete ON organization_members FOR DELETE TO app_runtime
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
CREATE POLICY resolver ON organization_members TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE (role, updated_at) ON organization_members TO app_resolver;

ALTER TABLE organization_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_invitations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON organization_invitations FROM app_runtime;
GRANT SELECT, INSERT, UPDATE (revoked_at) ON organization_invitations TO app_runtime;
CREATE POLICY own_or_context ON organization_invitations FOR SELECT TO app_runtime
  USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
         OR organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
CREATE POLICY context_insert ON organization_invitations FOR INSERT TO app_runtime
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
CREATE POLICY context_update ON organization_invitations FOR UPDATE TO app_runtime
  USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)
  WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid);
CREATE POLICY resolver ON organization_invitations TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, UPDATE (accepted_at, declined_at) ON organization_invitations TO app_resolver;

-- Members of an organization see each other's names in its context (like coworkers in a workspace).
CREATE POLICY organization_coworker ON users FOR SELECT TO app_runtime
  USING (id IN (SELECT m.user_id FROM organization_members m
                WHERE m.organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid));

-- The organization sync writes memberships of the organization's workspaces as app_resolver.
CREATE POLICY resolver_org_sync ON memberships TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE (role, via_organization, updated_at), DELETE ON memberships TO app_resolver;
GRANT SELECT (id, namespace_id, deleted_at) ON workspaces TO app_resolver;
GRANT UPDATE (revoked_at) ON oauth_grants TO app_resolver;
CREATE POLICY resolver_revoke ON oauth_grants FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);
GRANT UPDATE (revoked_at) ON access_tokens TO app_resolver;
GRANT SELECT (workspace_id, user_id) ON access_tokens TO app_resolver;
GRANT SELECT (workspace_id, user_id) ON oauth_grants TO app_resolver;

DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY[
    'free_handle(text)', 'apply_org_role(uuid, uuid, text)', 'sync_org_access(uuid, uuid)',
    'organization_members_sync()', 'workspaces_org_access()', 'organization_members_keep_owner()',
    'create_user_with_identity(text, text, text, text, text, text)', 'create_workspace(text, text, uuid)',
    'create_organization(text, text)', 'rename_namespace(uuid, text)', 'retire_namespace(uuid)',
    'answer_organization_invitation(uuid, boolean)', 'sweep_handle_reservations()',
    'handle_unavailable(text, uuid)'] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
  END LOOP;
  FOREACH f IN ARRAY ARRAY[
    'create_user_with_identity(text, text, text, text, text, text)', 'create_workspace(text, text, uuid)',
    'create_organization(text, text)', 'rename_namespace(uuid, text)', 'retire_namespace(uuid)',
    'answer_organization_invitation(uuid, boolean)', 'sweep_handle_reservations()', 'free_handle(text)',
    'handle_unavailable(text, uuid)'] LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;
GRANT EXECUTE ON FUNCTION app_private.handle_reserved(text), app_private.valid_handle(text),
  app_private.role_rank(text) TO app_runtime, app_resolver;
