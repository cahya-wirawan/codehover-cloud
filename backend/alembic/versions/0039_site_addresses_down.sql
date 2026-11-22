-- Back to free-form slugs; the revision then runs 0036_slug_reservations.sql again for the reservations.
DROP FUNCTION app_private.oauth_begin(uuid, text, text, text, text, text);
DROP FUNCTION app_private.oauth_pending(uuid);
DELETE FROM oauth_authorizations;  -- pending requests only last minutes; grants keep their project
ALTER TABLE oauth_authorizations DROP CONSTRAINT oauth_authorizations_site_check;
ALTER TABLE oauth_authorizations RENAME COLUMN site TO slug;
ALTER TABLE oauth_authorizations ADD CONSTRAINT oauth_authorizations_slug_check CHECK (slug ~ '^[a-z0-9][a-z0-9-]{2,62}$');
CREATE FUNCTION app_private.oauth_begin(p_client uuid, p_redirect_uri text, p_challenge text, p_resource text,
                                        p_slug text, p_state text)
  RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  INSERT INTO public.oauth_authorizations(client, redirect_uri, code_challenge, resource, slug, state)
  VALUES (p_client, p_redirect_uri, p_challenge, p_resource, p_slug, p_state) RETURNING id
$$;
CREATE FUNCTION app_private.oauth_pending(p_id uuid)
  RETURNS TABLE(client_name text, redirect_uri text, slug text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT c.client_name, a.redirect_uri, a.slug FROM public.oauth_authorizations a
  JOIN public.oauth_clients c ON c.id = a.client
  WHERE a.id = p_id AND a.decided_at IS NULL AND a.expires_at > now()
$$;
ALTER FUNCTION app_private.oauth_begin(uuid, text, text, text, text, text) OWNER TO app_resolver;
ALTER FUNCTION app_private.oauth_pending(uuid) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.oauth_begin(uuid, text, text, text, text, text), app_private.oauth_pending(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.oauth_begin(uuid, text, text, text, text, text), app_private.oauth_pending(uuid) TO app_runtime;

DROP FUNCTION app_private.resolve_site(text, text);
REVOKE SELECT (id, status) ON organizations FROM app_resolver;
REVOKE SELECT (slug, namespace_id) ON projects FROM app_resolver;

-- Free-form slugs again, taken from the aliases where there are; other public sites get a generated one.
ALTER TABLE publications ADD COLUMN public_slug text CHECK (public_slug IS NULL OR public_slug ~ '^[a-z0-9][a-z0-9-]{2,62}$');
UPDATE publications p SET public_slug = (SELECT a.alias FROM site_aliases a WHERE a.project_id = p.project_id
                                          ORDER BY a.created_at, a.alias LIMIT 1);
UPDATE publications p SET public_slug = 'site-' || left(replace(p.project_id::text, '-', ''), 12)
 WHERE p.public_slug IS NULL AND p.visibility = 'public';
ALTER TABLE publications ADD CONSTRAINT publications_public_slug_key UNIQUE (public_slug);
ALTER TABLE publications ADD CONSTRAINT publications_check CHECK (visibility = 'private' OR public_slug IS NOT NULL);
GRANT SELECT (public_slug) ON publications TO app_resolver;
CREATE FUNCTION app_private.resolve_publication(slug text)
  RETURNS TABLE (workspace_id uuid, project_id uuid, build_id uuid, visibility text)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.workspace_id, p.project_id, p.build_id, p.visibility
  FROM public.publications p
  JOIN public.workspaces w ON w.id = p.workspace_id AND w.status = 'active'
  JOIN public.projects pr ON pr.id = p.project_id AND pr.deleted_at IS NULL
  WHERE p.public_slug = $1 AND p.suspended_at IS NULL  -- $1: projects also has a column named slug
$$;
ALTER FUNCTION app_private.resolve_publication(text) OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.resolve_publication(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_private.resolve_publication(text) TO app_runtime;

DROP FUNCTION app_private.site_alias(text);
DROP TABLE site_aliases;
DROP TRIGGER projects_namespace ON projects;
DROP FUNCTION app_private.projects_namespace();
DROP INDEX uq_projects_namespace_slug;
ALTER TABLE projects DROP COLUMN namespace_id;
