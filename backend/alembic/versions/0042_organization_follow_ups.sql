-- 0042 organization follow-ups (02 S03b, S16; 06 organizations, billing, workspaces).
--
-- 1. Member details: an organization's owners and admins see when each member was last active and which
--    of the organization's workspaces they are in, with their role there.
-- 2. Billing handover: an owner's billing rows (its Stripe customer, subscriptions and grants) are created
--    through one workspace, the anchor, which cannot be deleted while it holds a live subscription. Whoever
--    manages the owner's plan may hand the rows to another live workspace of the same owner.
-- 3. Moving a workspace to another owner (a person's own namespace, or an organization the caller owns or
--    administers). Its projects keep their slugs under the new owner; each old address keeps leading to
--    its project for 90 days (project_slug_redirects in the old namespace, so the old owner's other
--    workspaces cannot take it meanwhile). A slug the new owner already uses refuses the move. A workspace
--    that holds billing rows hands them over first. Organization access follows the new owner.

-- ---------------------------------------------------------------- 1. member details
GRANT SELECT (name) ON workspaces TO app_resolver;

CREATE FUNCTION app_private.organization_member_details(p_org uuid)
  RETURNS TABLE (user_id uuid, last_active_at timestamptz, workspaces jsonb)
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
BEGIN
  IF uid IS NULL OR NOT EXISTS (
       SELECT 1 FROM public.organization_members m JOIN public.organizations o ON o.id = m.organization_id
        WHERE m.organization_id = p_org AND m.user_id = uid AND m.role IN ('owner', 'admin')
          AND o.status = 'active') THEN
    RAISE EXCEPTION 'only owners and admins see member details' USING ERRCODE = '42501';
  END IF;
  RETURN QUERY
  SELECT om.user_id,
         (SELECT max(s.last_seen_at) FROM public.sessions s WHERE s.user_id = om.user_id),
         COALESCE((
           SELECT jsonb_agg(jsonb_build_object('id', w.id, 'name', w.name, 'role', ms.role,
                                               'via_organization', ms.via_organization) ORDER BY w.name, w.id)
             FROM public.memberships ms
             JOIN public.workspaces w ON w.id = ms.workspace_id AND w.deleted_at IS NULL
             JOIN public.namespaces n ON n.id = w.namespace_id AND n.organization_id = p_org
            WHERE ms.user_id = om.user_id), '[]'::jsonb)
    FROM public.organization_members om
   WHERE om.organization_id = p_org;
END $$;

-- ---------------------------------------------------------------- 2. billing handover
-- Billing rows never move to another owner, and move to another workspace of the same owner only through
-- app_private.move_billing_anchor, which names the receiving workspace in a transaction-local setting.
-- (The runtime's row policies already pin a row's workspace on update.)
CREATE OR REPLACE FUNCTION app_private.billing_namespace() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.namespace_id := (SELECT w.namespace_id FROM public.workspaces w WHERE w.id = NEW.workspace_id);
  ELSE
    NEW.namespace_id := OLD.namespace_id;  -- never moves to another owner
    -- COALESCE: an unset setting or a missing workspace must reset, never leave the comparison NULL.
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id AND NOT COALESCE(
         current_setting('codehover.billing_anchor', true) = NEW.workspace_id::text
         AND (SELECT w.namespace_id FROM public.workspaces w
               WHERE w.id = NEW.workspace_id AND w.deleted_at IS NULL) = OLD.namespace_id, false) THEN
      NEW.workspace_id := OLD.workspace_id;  -- nor to another workspace except by a handover
    END IF;
  END IF;
  RETURN NEW;
END $$;

GRANT SELECT (namespace_id) ON billing_customers TO app_resolver;
GRANT UPDATE (workspace_id) ON billing_customers, subscriptions, entitlement_grants TO app_resolver;
GRANT SELECT (namespace_id) ON entitlement_grants TO app_resolver;
GRANT SELECT (updated_at) ON subscriptions TO app_resolver;
CREATE POLICY resolver_anchor ON billing_customers FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);
CREATE POLICY resolver_anchor ON subscriptions FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);

-- Hand the owner's billing rows to `p_workspace`; the caller (app.user_id) must manage the owner's plan.
-- Returns the workspace that held them before (as billing.service.billing_holder; NULL when none did).
CREATE FUNCTION app_private.move_billing_anchor(p_workspace uuid) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  caller uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
  owner_ns uuid;
  previous uuid;
BEGIN
  SELECT w.namespace_id INTO owner_ns FROM public.workspaces w
   WHERE w.id = p_workspace AND w.deleted_at IS NULL AND w.status = 'active';
  IF owner_ns IS NULL OR caller IS NULL OR NOT EXISTS (
       SELECT 1 FROM public.namespaces n WHERE n.id = owner_ns
          AND ((n.kind = 'user' AND n.user_id = caller)
            OR (n.kind = 'organization' AND EXISTS (SELECT 1 FROM public.organization_members m
                  WHERE m.organization_id = n.organization_id AND m.user_id = caller AND m.role = 'owner')))) THEN
    RAISE EXCEPTION 'only the person, or an owner of the organization, hands over its billing' USING ERRCODE = '42501';
  END IF;
  -- Serialized with checkouts, trials and spending of the owner.
  PERFORM pg_advisory_xact_lock(hashtextextended('billing-account:' || owner_ns::text, 0));
  previous := COALESCE(
    (SELECT c.workspace_id FROM public.billing_customers c WHERE c.namespace_id = owner_ns),
    (SELECT s.workspace_id FROM public.subscriptions s WHERE s.namespace_id = owner_ns
      ORDER BY s.updated_at DESC LIMIT 1),
    (SELECT g.workspace_id FROM public.entitlement_grants g WHERE g.namespace_id = owner_ns
      ORDER BY g.created_at DESC LIMIT 1));
  PERFORM set_config('codehover.billing_anchor', p_workspace::text, true);
  UPDATE public.billing_customers c SET workspace_id = p_workspace
   WHERE c.namespace_id = owner_ns AND c.workspace_id <> p_workspace;
  UPDATE public.subscriptions s SET workspace_id = p_workspace
   WHERE s.namespace_id = owner_ns AND s.workspace_id <> p_workspace;
  UPDATE public.entitlement_grants g SET workspace_id = p_workspace
   WHERE g.namespace_id = owner_ns AND g.workspace_id <> p_workspace;
  PERFORM set_config('codehover.billing_anchor', '', true);
  RETURN previous;
END $$;

-- ---------------------------------------------------------------- 3. moving a workspace
GRANT UPDATE (namespace_id, updated_at) ON workspaces TO app_resolver;
GRANT UPDATE (namespace_id) ON projects TO app_resolver;
GRANT SELECT (name, slug, namespace_id) ON projects TO app_resolver;
CREATE POLICY resolver_move ON workspaces FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);

-- The live projects of `p_workspace` whose slug the owner `p_target` already uses: a live project of
-- another workspace, or a redirect of one that still lasts. VOLATILE: called after waiting for locks.
CREATE FUNCTION app_private.workspace_move_clashes(p_workspace uuid, p_target uuid)
  RETURNS TABLE (project_id uuid, name text, slug text)
  LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.id, p.name, p.slug FROM public.projects p
   WHERE p.workspace_id = p_workspace AND p.deleted_at IS NULL
     AND (EXISTS (SELECT 1 FROM public.projects q
                   WHERE q.namespace_id = p_target AND q.slug = p.slug AND q.deleted_at IS NULL
                     AND q.workspace_id <> p_workspace)
       OR EXISTS (SELECT 1 FROM public.project_slug_redirects r JOIN public.projects pr ON pr.id = r.project_id
                   WHERE r.namespace_id = p_target AND r.slug = p.slug AND r.reserved_until > now()
                     AND pr.workspace_id <> p_workspace))
   ORDER BY p.slug
$$;

-- Move `p_workspace` to the caller's own namespace (`p_organization` NULL) or to an organization the
-- caller owns or administers. The caller (app.user_id) must own what it leaves: their own namespace
-- (never another person's: a co-owner cannot take it), or, for an organization's workspace, be an owner
-- of that organization (a member given a direct owner role there cannot take it out; security review),
-- and be an owner of the workspace. Returns the new
-- owner's namespace. Errors: 42501 not allowed, 22023 already the owner, 55000 the workspace holds billing
-- rows, 23505 (uq_projects_namespace_slug) a project's slug is taken there.
CREATE FUNCTION app_private.move_workspace(p_workspace uuid, p_organization uuid) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  caller uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
  src uuid;
  dst uuid;
  src_org uuid;
  dst_org uuid;
  p record;
  om record;
BEGIN
  IF caller IS NULL THEN
    RAISE EXCEPTION 'app.user_id not set' USING ERRCODE = '42501';
  END IF;
  SELECT w.namespace_id INTO src FROM public.workspaces w
   WHERE w.id = p_workspace AND w.deleted_at IS NULL AND w.status = 'active' FOR UPDATE;
  IF src IS NULL OR NOT EXISTS (SELECT 1 FROM public.memberships m
                                 WHERE m.workspace_id = p_workspace AND m.user_id = caller AND m.role = 'owner') THEN
    RAISE EXCEPTION 'only an owner of the workspace moves it' USING ERRCODE = '42501';
  END IF;
  IF p_organization IS NULL THEN
    SELECT n.id INTO dst FROM public.namespaces n WHERE n.user_id = caller;
  ELSE
    SELECT n.id INTO dst FROM public.namespaces n
      JOIN public.organizations o ON o.id = n.organization_id AND o.status = 'active'
      JOIN public.organization_members m ON m.organization_id = o.id AND m.user_id = caller
                                        AND m.role IN ('owner', 'admin')
     WHERE n.organization_id = p_organization;
  END IF;
  IF dst IS NULL THEN
    RAISE EXCEPTION 'not an owner or admin of this organization' USING ERRCODE = '42501';
  END IF;
  IF dst = src THEN
    RAISE EXCEPTION 'the workspace already belongs to this owner' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.namespaces n
                  WHERE n.id = src
                    AND ((n.kind = 'user' AND n.user_id = caller)
                      OR (n.kind = 'organization' AND EXISTS (
                            SELECT 1 FROM public.organization_members m
                             WHERE m.organization_id = n.organization_id AND m.user_id = caller AND m.role = 'owner')))) THEN
    RAISE EXCEPTION 'only the workspace''s owner moves it' USING ERRCODE = '42501';
  END IF;
  -- Both owners' allowances and limits, in a fixed order (spending and creates take the same locks).
  PERFORM pg_advisory_xact_lock(hashtextextended('billing-account:' || LEAST(src, dst)::text, 0));
  PERFORM pg_advisory_xact_lock(hashtextextended('billing-account:' || GREATEST(src, dst)::text, 0));
  IF EXISTS (SELECT 1 FROM public.billing_customers c WHERE c.workspace_id = p_workspace)
     OR EXISTS (SELECT 1 FROM public.subscriptions s WHERE s.workspace_id = p_workspace)
     OR EXISTS (SELECT 1 FROM public.entitlement_grants g WHERE g.workspace_id = p_workspace) THEN
    RAISE EXCEPTION 'the workspace holds its owner''s billing' USING ERRCODE = '55000';
  END IF;
  -- Each address under the new owner, in slug order, against new projects and renames there.
  FOR p IN SELECT DISTINCT x.slug FROM public.projects x
            WHERE x.workspace_id = p_workspace AND x.deleted_at IS NULL ORDER BY x.slug LOOP
    PERFORM pg_advisory_xact_lock(hashtextextended('project-slug:' || dst::text || '/' || p.slug, 0));
  END LOOP;
  IF EXISTS (SELECT 1 FROM app_private.workspace_move_clashes(p_workspace, dst)) THEN
    RAISE EXCEPTION 'a project address is taken under the new owner' USING ERRCODE = '23505',
      CONSTRAINT = 'uq_projects_namespace_slug';
  END IF;
  SELECT n.organization_id INTO src_org FROM public.namespaces n WHERE n.id = src;
  SELECT n.organization_id INTO dst_org FROM public.namespaces n WHERE n.id = dst;

  -- Moved to the person: they own it directly from now on.
  IF p_organization IS NULL THEN
    UPDATE public.memberships m SET role = 'owner', via_organization = false, updated_at = now()
     WHERE m.workspace_id = p_workspace AND m.user_id = caller;
  END IF;
  -- The old organization's owners and admins lose what it gave them (unless the new one gives it again).
  IF src_org IS NOT NULL THEN
    FOR om IN SELECT x.user_id FROM public.organization_members x
               WHERE x.organization_id = src_org AND x.role IN ('owner', 'admin')
                 AND NOT EXISTS (SELECT 1 FROM public.organization_members y
                                  WHERE y.organization_id = dst_org AND y.user_id = x.user_id
                                    AND y.role IN ('owner', 'admin'))
               ORDER BY x.user_id LOOP
      PERFORM app_private.apply_org_role(p_workspace, om.user_id, NULL);
    END LOOP;
  END IF;

  -- The old addresses lead to the projects for 90 days; an old redirect of theirs under the new owner
  -- (moving back) ends.
  FOR p IN SELECT x.id, x.slug FROM public.projects x
            WHERE x.workspace_id = p_workspace AND x.deleted_at IS NULL LOOP
    INSERT INTO public.project_slug_redirects(namespace_id, slug, project_id, released_at, reserved_until)
    VALUES (src, p.slug, p.id, clock_timestamp(), clock_timestamp() + interval '90 days')
    ON CONFLICT (namespace_id, slug) DO UPDATE SET project_id = EXCLUDED.project_id,
      released_at = EXCLUDED.released_at, reserved_until = EXCLUDED.reserved_until;
    DELETE FROM public.project_slug_redirects r WHERE r.namespace_id = dst AND r.slug = p.slug;
    DELETE FROM public.project_slug_redirects r WHERE r.project_id = p.id
       AND (r.namespace_id, r.slug) IN (SELECT y.namespace_id, y.slug FROM public.project_slug_redirects y
                                         WHERE y.project_id = p.id
                                         ORDER BY y.released_at DESC, y.slug OFFSET 3);
  END LOOP;
  UPDATE public.projects x SET namespace_id = dst WHERE x.workspace_id = p_workspace;
  UPDATE public.workspaces w SET namespace_id = dst, updated_at = now() WHERE w.id = p_workspace;

  -- The new organization's owners and admins, as for a workspace created in it.
  IF dst_org IS NOT NULL THEN
    FOR om IN SELECT x.user_id, x.role FROM public.organization_members x
               WHERE x.organization_id = dst_org AND x.role IN ('owner', 'admin') ORDER BY x.user_id LOOP
      PERFORM app_private.apply_org_role(p_workspace, om.user_id, om.role);
    END LOOP;
  END IF;
  RETURN dst;
END $$;

-- Resolving an address (replaces 0041's): as before, but the site's owner is the project's current owner,
-- so a moved workspace's old addresses (redirects in the old namespace) lead to the new owner's address.
CREATE OR REPLACE FUNCTION app_private.resolve_site(p_owner text, p_project text)
  RETURNS TABLE (workspace_id uuid, project_id uuid, build_id uuid, visibility text, owner text, project text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  WITH owner_ns AS (
    SELECT n.id FROM public.namespaces n
     WHERE n.id = COALESCE(
             (SELECT x.id FROM public.namespaces x WHERE x.handle = p_owner),
             (SELECT r.namespace_id FROM public.handle_reservations r WHERE r.handle = p_owner AND r.reserved_until > now()))
       AND (n.organization_id IS NULL OR EXISTS (SELECT 1 FROM public.organizations o
                                                  WHERE o.id = n.organization_id AND o.status = 'active'))),
  target AS (
    SELECT COALESCE(
      (SELECT pr.id FROM public.projects pr, owner_ns
        WHERE pr.namespace_id = owner_ns.id AND pr.slug = p_project AND pr.deleted_at IS NULL),
      (SELECT r.project_id FROM public.project_slug_redirects r, owner_ns
        WHERE r.namespace_id = owner_ns.id AND r.slug = p_project AND r.reserved_until > now())) AS id)
  SELECT p.workspace_id, p.project_id, p.build_id, p.visibility, cur.handle, pr.slug
  FROM target
  JOIN public.projects pr ON pr.id = target.id AND pr.deleted_at IS NULL
  JOIN public.namespaces cur ON cur.id = pr.namespace_id
   AND (cur.organization_id IS NULL OR EXISTS (SELECT 1 FROM public.organizations o
                                                WHERE o.id = cur.organization_id AND o.status = 'active'))
  JOIN public.workspaces w ON w.id = pr.workspace_id AND w.status = 'active'
  JOIN public.publications p ON p.project_id = pr.id AND p.suspended_at IS NULL
$$;

DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY['organization_member_details(uuid)', 'move_billing_anchor(uuid)',
                           'workspace_move_clashes(uuid, uuid)', 'move_workspace(uuid, uuid)'] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;
