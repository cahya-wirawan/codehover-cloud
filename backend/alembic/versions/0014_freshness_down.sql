ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_observation_pair;
ALTER TABLE projects DROP COLUMN IF EXISTS last_observed_at;
ALTER TABLE projects DROP COLUMN IF EXISTS last_observed_sha;
