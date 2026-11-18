-- 0010 publications, suspension on revoked access and the publication resolver (backlog T15; 05, 06, 09).

-- The mutable pointer from a project to one ready build (10: "Mutable publication pointer lives only in
-- PostgreSQL"). Readiness and the quality gate are checked by the service under a row lock, because a
-- foreign key cannot express them; the composite key still guarantees same-workspace, same-project.
CREATE TABLE publications (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  build_id uuid NOT NULL,
  visibility text NOT NULL CHECK (visibility IN ('private', 'public')),
  public_slug text CHECK (public_slug IS NULL OR public_slug ~ '^[a-z0-9][a-z0-9-]{2,62}$'),
  version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  acknowledged_partial boolean NOT NULL DEFAULT false,
  suspended_at timestamptz,
  suspended_reason text CHECK (suspended_reason IS NULL OR suspended_reason IN ('connection_inactive', 'repository_revoked', 'operator')),
  published_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (visibility = 'private' OR public_slug IS NOT NULL),
  CHECK ((suspended_at IS NULL) = (suspended_reason IS NULL)),
  UNIQUE (project_id),
  UNIQUE (public_slug),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, project_id, build_id) REFERENCES builds(workspace_id, project_id, id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON publications TO app_runtime;
CREATE POLICY tenant ON publications TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
ALTER TABLE publications ENABLE ROW LEVEL SECURITY;
ALTER TABLE publications FORCE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------- resolver (05 "Published sites")
-- Delivery learns only workspace, project, build and visibility of an unsuspended publication.
-- USING (true): an UPDATE that suspends a row must still see the new row; resolve_publication itself
-- filters suspended publications, and app_resolver is NOLOGIN, used only inside these functions.
CREATE POLICY resolver ON publications FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_suspend ON publications FOR UPDATE TO app_resolver USING (true) WITH CHECK (true);
GRANT SELECT (id, workspace_id, project_id, build_id, visibility, public_slug, suspended_at),
      UPDATE (suspended_at, suspended_reason, updated_at) ON publications TO app_resolver;

CREATE FUNCTION app_private.resolve_publication(slug text)
  RETURNS TABLE (workspace_id uuid, project_id uuid, build_id uuid, visibility text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.workspace_id, p.project_id, p.build_id, p.visibility
  FROM public.publications p WHERE p.public_slug = slug AND p.suspended_at IS NULL
$$;
ALTER FUNCTION app_private.resolve_publication(text) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.resolve_publication(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.resolve_publication(text) TO app_runtime;

-- ---------------------------------------------------------------- suspension (FR-027, 03)
-- Whatever path revokes repository authority (webhook, admin disconnect, narrower reconnect), every
-- publication of the affected projects stops serving at once, until an admin publishes again.
CREATE POLICY resolver_publication_lookup ON projects FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_publication_lookup ON repositories FOR SELECT TO app_resolver USING (true);
GRANT SELECT (id, repository_id) ON projects TO app_resolver;
GRANT SELECT (id, connection_id) ON repositories TO app_resolver;

CREATE FUNCTION app_private.suspend_publications() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF TG_TABLE_NAME = 'repository_connections' THEN
    IF NEW.status <> 'active' AND OLD.status = 'active' THEN
      UPDATE public.publications pub SET suspended_at = now(), suspended_reason = 'connection_inactive', updated_at = now()
      WHERE pub.suspended_at IS NULL AND pub.project_id IN (
        SELECT pr.id FROM public.projects pr JOIN public.repositories r ON r.id = pr.repository_id
        WHERE r.connection_id = NEW.id);
    END IF;
  ELSIF NEW.access_revoked_at IS NOT NULL AND OLD.access_revoked_at IS NULL THEN
    UPDATE public.publications pub SET suspended_at = now(), suspended_reason = 'repository_revoked', updated_at = now()
    WHERE pub.suspended_at IS NULL AND pub.project_id IN (SELECT pr.id FROM public.projects pr WHERE pr.repository_id = NEW.id);
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION app_private.suspend_publications() OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.suspend_publications() FROM PUBLIC;

CREATE TRIGGER publications_suspend_on_connection AFTER UPDATE OF status ON repository_connections
  FOR EACH ROW EXECUTE FUNCTION app_private.suspend_publications();
CREATE TRIGGER publications_suspend_on_repository AFTER UPDATE OF access_revoked_at ON repositories
  FOR EACH ROW EXECUTE FUNCTION app_private.suspend_publications();
