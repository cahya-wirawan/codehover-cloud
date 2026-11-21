-- Before 0032 no build ran beside another of its project: active family variants are cancelled first.
UPDATE builds SET status = 'cancelled', completed_at = now(), updated_at = now()
  WHERE status IN ('queued', 'running') AND (config->>'family') IS NOT NULL;
DROP INDEX uq_builds_one_active_per_project;
CREATE UNIQUE INDEX uq_builds_one_active_per_project ON builds(project_id) WHERE status IN ('queued', 'running');
