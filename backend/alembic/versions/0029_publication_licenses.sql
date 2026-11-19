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

-- Fails closed: unless the project's repository is App-granted or uploaded, a public publication needs a
-- recorded license. Runs as the caller (app_runtime, in the publishing transaction's workspace context).
CREATE FUNCTION app_private.publication_license_check() RETURNS trigger
  LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
  source text;
BEGIN
  IF NEW.visibility <> 'public' OR NEW.license_spdx IS NOT NULL THEN
    RETURN NEW;
  END IF;
  SELECT c.provider INTO source FROM public.projects p
    JOIN public.repositories r ON r.workspace_id = p.workspace_id AND r.id = p.repository_id
    JOIN public.repository_connections c ON c.workspace_id = r.workspace_id AND c.id = r.connection_id
   WHERE p.workspace_id = NEW.workspace_id AND p.id = NEW.project_id;
  IF source IS NULL OR source NOT IN ('github', 'upload') THEN
    RAISE EXCEPTION 'a public site of this repository needs a permitted license'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
REVOKE EXECUTE ON FUNCTION app_private.publication_license_check() FROM PUBLIC;  -- triggers need no grant

CREATE TRIGGER publication_license_check
  BEFORE INSERT OR UPDATE OF visibility, build_id, license_spdx, license_path ON publications
  FOR EACH ROW EXECUTE FUNCTION app_private.publication_license_check();
