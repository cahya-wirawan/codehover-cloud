DROP POLICY project_access ON projects;
DROP FUNCTION app_private.project_role(uuid, uuid, uuid);
REVOKE SELECT (access) ON projects FROM app_resolver;
DROP TABLE project_members;
ALTER TABLE projects DROP COLUMN access_version;
ALTER TABLE projects DROP COLUMN access;
