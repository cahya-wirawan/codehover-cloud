-- Uploaded projects have no GitHub identifiers. Downgrading keeps their data but turns their connection and
-- repositories into revoked, GitHub-shaped rows with synthetic identifiers far above GitHub's range, so the
-- older schema's NOT NULL columns hold and nothing can reach them through GitHub.
ALTER TABLE snapshots DROP CONSTRAINT snapshots_archive_key;
ALTER TABLE repository_connections DROP CONSTRAINT repository_connections_provider_fields;
UPDATE repository_connections SET installation_id = 9000000000000000 + (('x' || substr(md5(id::text), 1, 12))::bit(48)::bigint),
  github_account_id = 9000000000000000 + (('x' || substr(md5(id::text), 13, 12))::bit(48)::bigint),
  github_account_login = 'uploaded-archive', status = 'revoked'
  WHERE provider = 'upload';
UPDATE repositories r SET provider_repo_id = 9000000000000000 + (('x' || substr(md5(r.id::text), 1, 12))::bit(48)::bigint),
  access_revoked_at = COALESCE(r.access_revoked_at, now())
  FROM repository_connections c WHERE c.workspace_id = r.workspace_id AND c.id = r.connection_id AND c.provider = 'upload';
ALTER TABLE repositories ALTER COLUMN provider_repo_id SET NOT NULL;
DROP INDEX uq_repository_connections_upload;
ALTER TABLE repository_connections ALTER COLUMN github_account_login SET NOT NULL;
ALTER TABLE repository_connections ALTER COLUMN github_account_id SET NOT NULL;
ALTER TABLE repository_connections ALTER COLUMN installation_id SET NOT NULL;
ALTER TABLE repository_connections DROP COLUMN provider;
