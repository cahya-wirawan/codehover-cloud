-- 0028 Public GitHub repositories (01 "Public repository source", 08 "Public repository reads").
--
-- A project can read a public GitHub repository without the GitHub App. Its repository sits under the
-- workspace's one `public` connection, so every path that joins projects to repositories and connections
-- works unchanged. Repositories keep GitHub's numeric ID (renames never redirect access), and
-- UNIQUE (workspace_id, provider_repo_id) still holds: a later App grant of the same repository moves the
-- row to the App connection, and adding a public repository the App already grants reuses that row.

ALTER TABLE repository_connections DROP CONSTRAINT repository_connections_provider_check;
ALTER TABLE repository_connections ADD CONSTRAINT repository_connections_provider_check
  CHECK (provider IN ('github', 'upload', 'public'));
CREATE UNIQUE INDEX uq_repository_connections_public ON repository_connections(workspace_id) WHERE provider = 'public';
