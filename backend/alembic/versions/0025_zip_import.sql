-- 0025 ZIP repository import (M6; 01 P1 "ZIP repository import", 08 "Archive traversal").
--
-- An uploaded project has a repository under its workspace's one `upload` connection, so every path that
-- joins projects to repositories and connections (builds, publication, deletion, exports) works the same.
-- GitHub-only fields are empty for uploads; the archive itself lives under its snapshot's object prefix
-- (`snapshots.archive_key`), and its SHA-256 is the snapshot's commit identifier.

ALTER TABLE repository_connections ADD COLUMN provider text NOT NULL DEFAULT 'github'
  CHECK (provider IN ('github', 'upload'));
ALTER TABLE repository_connections ALTER COLUMN installation_id DROP NOT NULL;
ALTER TABLE repository_connections ALTER COLUMN github_account_id DROP NOT NULL;
ALTER TABLE repository_connections ALTER COLUMN github_account_login DROP NOT NULL;
ALTER TABLE repository_connections ADD CONSTRAINT repository_connections_provider_fields CHECK (
  (provider = 'github') = (installation_id IS NOT NULL AND github_account_id IS NOT NULL AND github_account_login IS NOT NULL)
);
CREATE UNIQUE INDEX uq_repository_connections_upload ON repository_connections(workspace_id) WHERE provider = 'upload';

ALTER TABLE repositories ALTER COLUMN provider_repo_id DROP NOT NULL;
ALTER TABLE snapshots ADD CONSTRAINT snapshots_archive_key CHECK (
  archive_key IS NULL OR archive_key ~ '^workspaces/[0-9a-f-]{36}/projects/[0-9a-f-]{36}/snapshots/[0-9a-f-]{36}/upload\.zip$'
);
