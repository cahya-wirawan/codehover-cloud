-- 0032: the variants a several-language build starts with its base (config.family, set only by the server)
-- run beside it; every other build, a variant started on its own included, keeps the rule of at most one
-- active build per project.
DROP INDEX uq_builds_one_active_per_project;
CREATE UNIQUE INDEX uq_builds_one_active_per_project ON builds(project_id)
  WHERE status IN ('queued', 'running') AND (config->>'family') IS NULL;
