-- 0014 last observed upstream commit for freshness (FR-019, 03 "Update and publication").

-- The latest SHA of the tracked ref as last seen from GitHub, and when. When GitHub is unreachable the
-- freshness report says "unknown" and shows this observation instead of claiming the site is current.
ALTER TABLE projects ADD COLUMN last_observed_sha text
  CHECK (last_observed_sha IS NULL OR last_observed_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$');
ALTER TABLE projects ADD COLUMN last_observed_at timestamptz;
ALTER TABLE projects ADD CONSTRAINT projects_observation_pair CHECK ((last_observed_sha IS NULL) = (last_observed_at IS NULL));
