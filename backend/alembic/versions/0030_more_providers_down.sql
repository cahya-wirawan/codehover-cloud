-- Keys for the newer providers are destroyed, not kept: an older release cannot use or show them.
DELETE FROM provider_keys WHERE provider NOT IN ('anthropic');
ALTER TABLE provider_keys DROP CONSTRAINT provider_keys_provider_check;
ALTER TABLE provider_keys ADD CONSTRAINT provider_keys_provider_check CHECK (provider IN ('anthropic'));
