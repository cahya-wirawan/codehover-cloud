-- 0029 Licenses of public sites of public-source repositories (01 FR-029, 21).
--
-- A public site of a repository read through the `public` connection redistributes someone else's code,
-- so publishing it needs a license that permits that. The service checks GitHub's license at the build's
-- commit (app.modules.projects.licenses) and records it on the publication; the site names it. The
-- trigger is the backstop: no path may make such a publication public without a recorded license.

ALTER TABLE publications
  ADD COLUMN license_spdx text CHECK (license_spdx IS NULL OR license_spdx ~ '^[A-Za-z0-9.+-]{1,64}$'),
  ADD COLUMN license_path text CHECK (license_path IS NULL OR length(license_path) BETWEEN 1 AND 1024),
  ADD CONSTRAINT publications_license_pair CHECK ((license_spdx IS NULL) = (license_path IS NULL));

ALTER TABLE publications DROP CONSTRAINT publications_suspended_reason_check;
ALTER TABLE publications ADD CONSTRAINT publications_suspended_reason_check
  CHECK (suspended_reason IS NULL OR suspended_reason IN
         ('connection_inactive', 'repository_revoked', 'operator', 'billing', 'license'));

-- Public sites of public-source repositories published before this check stop serving until an admin
-- publishes again, which checks the license.
UPDATE publications pub SET suspended_at = now(), suspended_reason = 'license', updated_at = now()
  FROM projects p
  JOIN repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id
  JOIN repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id
  WHERE p.workspace_id = pub.workspace_id AND p.id = pub.project_id
    AND pub.visibility = 'public' AND pub.suspended_at IS NULL AND c.provider = 'public';

-- Fails closed: unless the project's repository is App-granted or uploaded, a public publication without a
-- recorded license may not serve. Making one public (insert, visibility, build or license change) is refused.
-- Lifting a suspension of one (the billing sync, an operator reinstating a takedown) keeps it suspended as
-- `license` instead, so a public site that was already suspended when this check arrived, and so missed the
-- backfill above, never comes back unchecked; only a publish through the service, with the check, does.
-- SECURITY DEFINER as app_resolver, so it reads the source the same way for every caller.
CREATE FUNCTION app_private.publication_license_check() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
  source text;
BEGIN
  IF NEW.visibility <> 'public' OR NEW.license_spdx IS NOT NULL OR NEW.suspended_at IS NOT NULL THEN
    RETURN NEW;
  END IF;
  SELECT c.provider INTO source FROM public.projects p
    JOIN public.repositories r ON r.id = p.repository_id
    JOIN public.repository_connections c ON c.id = r.connection_id
   WHERE p.id = NEW.project_id;
  IF source IN ('github', 'upload') THEN
    RETURN NEW;
  END IF;
  IF TG_OP = 'UPDATE' AND OLD.suspended_at IS NOT NULL THEN
    NEW.suspended_at := OLD.suspended_at;
    NEW.suspended_reason := 'license';
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'a public site of this repository needs a permitted license'
    USING ERRCODE = 'check_violation';
END $$;
GRANT SELECT (provider) ON repository_connections TO app_resolver;
ALTER FUNCTION app_private.publication_license_check() OWNER TO app_resolver;
REVOKE EXECUTE ON FUNCTION app_private.publication_license_check() FROM PUBLIC;  -- triggers need no grant

CREATE TRIGGER publication_license_check
  BEFORE INSERT OR UPDATE OF visibility, build_id, license_spdx, license_path, suspended_at ON publications
  FOR EACH ROW EXECUTE FUNCTION app_private.publication_license_check();
