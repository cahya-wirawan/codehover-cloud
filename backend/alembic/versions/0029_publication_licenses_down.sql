-- License suspensions stay suspended under an older reason (republishing clears it), so a downgrade never
-- brings an unchecked public site back online.
DROP TRIGGER publication_license_check ON publications;
DROP FUNCTION app_private.publication_license_check();
REVOKE SELECT (provider) ON repository_connections FROM app_resolver;
UPDATE publications SET suspended_reason = 'repository_revoked' WHERE suspended_reason = 'license';
ALTER TABLE publications DROP CONSTRAINT publications_suspended_reason_check;
ALTER TABLE publications ADD CONSTRAINT publications_suspended_reason_check
  CHECK (suspended_reason IS NULL OR suspended_reason IN ('connection_inactive', 'repository_revoked', 'operator', 'billing'));
ALTER TABLE publications DROP CONSTRAINT publications_license_pair,
  DROP COLUMN license_path, DROP COLUMN license_spdx;
