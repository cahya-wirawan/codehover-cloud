-- Public connections become revoked, GitHub-shaped rows with synthetic identifiers far above GitHub's range
-- (as 0025's downgrade does for uploads), so the older CHECK holds and nothing reaches them any more.
DROP INDEX uq_repository_connections_public;
UPDATE repository_connections SET provider = 'github',
  installation_id = 9100000000000000 + (('x' || substr(md5(id::text), 1, 12))::bit(48)::bigint),
  github_account_id = 9100000000000000 + (('x' || substr(md5(id::text), 13, 12))::bit(48)::bigint),
  github_account_login = 'public-repositories', status = 'revoked'
  WHERE provider = 'public';
ALTER TABLE repository_connections DROP CONSTRAINT repository_connections_provider_check;
ALTER TABLE repository_connections ADD CONSTRAINT repository_connections_provider_check
  CHECK (provider IN ('github', 'upload'));
