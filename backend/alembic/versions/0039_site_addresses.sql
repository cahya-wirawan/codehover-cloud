-- 0039 site addresses under their owner: /sites/<owner>/<project> (01 FR-030, 02 S20, 05, 06).
--
-- A published site was addressed by a free-form, global `publications.public_slug`. Now every
-- publication's address is its owner's handle (0038) and the project's slug, which becomes unique among
-- the live projects of one owner (across all of the owner's workspaces). An address can only ever name
-- its owner's project, so the 0036 reservations of released free-form slugs are no longer needed.
--
-- Old links keep working: each free-form slug in use (and each still reserved by 0036) becomes a
-- `site_aliases` row that redirects to its project's address for a year, and the old one-segment MCP
-- path answers through it. A renamed owner keeps answering at the old handle while it stays reserved for
-- them (90 days, 0038), so links and MCP clients move without breaking and nobody else can take them.

-- ---------------------------------------------------------------- project slugs per owner
ALTER TABLE projects ADD COLUMN namespace_id uuid REFERENCES namespaces(id);
UPDATE projects p SET namespace_id = w.namespace_id FROM workspaces w WHERE w.id = p.workspace_id;

-- Live projects of one owner sharing a slug (in different workspaces): the later ones get a suffix.
DO $$
DECLARE r record; candidate text; n integer;
BEGIN
  FOR r IN SELECT x.id, x.namespace_id, x.slug FROM (
             SELECT p.id, p.namespace_id, p.slug,
                    row_number() OVER (PARTITION BY p.namespace_id, p.slug ORDER BY p.created_at, p.id) AS k
               FROM public.projects p WHERE p.deleted_at IS NULL AND p.namespace_id IS NOT NULL) x
            WHERE x.k > 1 LOOP
    n := 2;
    LOOP
      candidate := left(r.slug, 44) || '-' || n;
      EXIT WHEN NOT EXISTS (SELECT 1 FROM public.projects p WHERE p.namespace_id = r.namespace_id
                              AND p.slug = candidate AND p.deleted_at IS NULL);
      n := n + 1;
    END LOOP;
    UPDATE public.projects p SET slug = candidate, updated_at = now() WHERE p.id = r.id;
  END LOOP;
END $$;
CREATE UNIQUE INDEX uq_projects_namespace_slug ON projects(namespace_id, slug) WHERE deleted_at IS NULL;

-- A new project takes its workspace's owner.
CREATE FUNCTION app_private.projects_namespace() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  NEW.namespace_id := (SELECT w.namespace_id FROM public.workspaces w WHERE w.id = NEW.workspace_id);
  RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION app_private.projects_namespace() FROM PUBLIC;
CREATE TRIGGER projects_namespace BEFORE INSERT ON projects
  FOR EACH ROW EXECUTE FUNCTION app_private.projects_namespace();

-- ---------------------------------------------------------------- old addresses become aliases
CREATE TABLE site_aliases (
  alias text PRIMARY KEY CHECK (alias ~ '^[a-z0-9][a-z0-9-]{2,62}$'),
  project_id uuid NOT NULL REFERENCES projects(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now() + interval '365 days'
);
INSERT INTO site_aliases(alias, project_id)
  SELECT p.public_slug, p.project_id FROM publications p WHERE p.public_slug IS NOT NULL;
INSERT INTO site_aliases(alias, project_id)
  SELECT r.slug, r.project_id FROM slug_reservations r WHERE r.reserved_until > now()
  ON CONFLICT (alias) DO NOTHING;
ALTER TABLE site_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE site_aliases FORCE ROW LEVEL SECURITY;
REVOKE ALL ON site_aliases FROM app_runtime;
CREATE POLICY resolver ON site_aliases TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT, DELETE ON site_aliases TO app_resolver;

-- The site an old one-segment slug now leads to: only while the alias lasts and the project is published
-- and serving. The caller shows a private one's address only to someone who may see the project
-- (security review: an old members-only slug must not reveal the owner and project to anyone else).
CREATE FUNCTION app_private.site_alias(p_alias text)
  RETURNS TABLE(owner text, project text, workspace_id uuid, project_id uuid, visibility text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT n.handle, pr.slug, pr.workspace_id, pr.id, p.visibility FROM public.site_aliases a
  JOIN public.projects pr ON pr.id = a.project_id AND pr.deleted_at IS NULL
  JOIN public.namespaces n ON n.id = pr.namespace_id
  JOIN public.workspaces w ON w.id = pr.workspace_id AND w.status = 'active'
  JOIN public.publications p ON p.project_id = pr.id AND p.suspended_at IS NULL
  WHERE a.alias = p_alias AND a.expires_at > now()
$$;

-- ---------------------------------------------------------------- the free-form slug goes
DROP TRIGGER publications_slug_claim ON publications;
DROP TRIGGER publications_slug_release ON publications;
DROP FUNCTION app_private.slug_claim();
DROP FUNCTION app_private.slug_release();
DROP FUNCTION app_private.sweep_slug_reservations();
DROP FUNCTION app_private.ops_release_slug(text, text);
DROP TABLE slug_reservations;
DROP FUNCTION app_private.resolve_publication(text);
ALTER TABLE publications DROP COLUMN public_slug;  -- with its unique index and checks

-- ---------------------------------------------------------------- resolving an address
GRANT SELECT (id, workspace_id, slug, namespace_id, deleted_at) ON projects TO app_resolver;
GRANT SELECT (id, status) ON organizations TO app_resolver;
-- Delivery learns workspace, project, build and visibility of an unsuspended publication of a live
-- project in an active workspace of a live owner, plus the owner's current handle (which differs from
-- `p_owner` when the address uses a handle the owner renamed and still holds reserved).
CREATE FUNCTION app_private.resolve_site(p_owner text, p_project text)
  RETURNS TABLE (workspace_id uuid, project_id uuid, build_id uuid, visibility text, owner text, project text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.workspace_id, p.project_id, p.build_id, p.visibility, n.handle, pr.slug
  FROM public.namespaces n
  JOIN public.projects pr ON pr.namespace_id = n.id AND pr.slug = p_project AND pr.deleted_at IS NULL
  JOIN public.workspaces w ON w.id = pr.workspace_id AND w.status = 'active'
  JOIN public.publications p ON p.project_id = pr.id AND p.suspended_at IS NULL
  WHERE n.id = COALESCE(
          (SELECT x.id FROM public.namespaces x WHERE x.handle = p_owner),
          (SELECT r.namespace_id FROM public.handle_reservations r WHERE r.handle = p_owner AND r.reserved_until > now()))
    AND (n.organization_id IS NULL OR EXISTS (SELECT 1 FROM public.organizations o
                                               WHERE o.id = n.organization_id AND o.status = 'active'))
$$;

-- ---------------------------------------------------------------- MCP sign-in names the address
ALTER TABLE oauth_authorizations DROP CONSTRAINT oauth_authorizations_slug_check;
ALTER TABLE oauth_authorizations RENAME COLUMN slug TO site;
-- Pending requests (10 minutes) start again; approved ones name their project's address. Grants are
-- bound to the project and keep working.
DELETE FROM oauth_authorizations WHERE project_id IS NULL;
UPDATE oauth_authorizations a SET site = n.handle || '/' || pr.slug
  FROM projects pr JOIN namespaces n ON n.id = pr.namespace_id WHERE pr.id = a.project_id;
DELETE FROM oauth_authorizations WHERE site !~ '^[a-z0-9]+(-[a-z0-9]+)*/[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$';
ALTER TABLE oauth_authorizations ADD CONSTRAINT oauth_authorizations_site_check
  CHECK (site ~ '^[a-z0-9]+(-[a-z0-9]+)*/[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$');
DROP FUNCTION app_private.oauth_begin(uuid, text, text, text, text, text);
DROP FUNCTION app_private.oauth_pending(uuid);
CREATE FUNCTION app_private.oauth_begin(p_client uuid, p_redirect_uri text, p_challenge text, p_resource text,
                                        p_site text, p_state text)
  RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  INSERT INTO public.oauth_authorizations(client, redirect_uri, code_challenge, resource, site, state)
  VALUES (p_client, p_redirect_uri, p_challenge, p_resource, p_site, p_state) RETURNING id
$$;
CREATE FUNCTION app_private.oauth_pending(p_id uuid)
  RETURNS TABLE(client_name text, redirect_uri text, site text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.client_name, a.redirect_uri, a.site FROM public.oauth_authorizations a
  JOIN public.oauth_clients c ON c.id = a.client
  WHERE a.id = p_id AND a.decided_at IS NULL AND a.expires_at > now()
$$;

DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY['projects_namespace()', 'site_alias(text)', 'resolve_site(text, text)',
                           'oauth_begin(uuid, text, text, text, text, text)', 'oauth_pending(uuid)'] LOOP
    EXECUTE format('ALTER FUNCTION app_private.%s OWNER TO app_resolver', f);
    EXECUTE format('REVOKE EXECUTE ON FUNCTION app_private.%s FROM PUBLIC', f);
  END LOOP;
  FOREACH f IN ARRAY ARRAY['site_alias(text)', 'resolve_site(text, text)',
                           'oauth_begin(uuid, text, text, text, text, text)', 'oauth_pending(uuid)'] LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION app_private.%s TO app_runtime', f);
  END LOOP;
END $$;
