-- 0004 repository grants (security review of T05; 09 "Repository authorization").
-- A connection grants only repositories the connecting user could read at connect time
-- (GitHub: GET /user/installations/{id}/repositories with the App's user token, i.e. the intersection
-- of the user's and the installation's access). Repositories outside that set are marked revoked
-- rather than deleted, because projects may reference them.
ALTER TABLE repositories ADD COLUMN access_revoked_at timestamptz;
CREATE INDEX ix_repositories_connection_granted ON repositories(connection_id) WHERE access_revoked_at IS NULL;
