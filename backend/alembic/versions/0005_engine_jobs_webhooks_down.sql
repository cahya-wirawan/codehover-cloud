DROP FUNCTION app_private.set_installation_status(bigint, text);
REVOKE SELECT, UPDATE ON repository_connections FROM app_resolver;
DROP POLICY resolver_lifecycle_update ON repository_connections;
DROP POLICY resolver_lifecycle ON repository_connections;
DROP TABLE webhook_receipts, reference_edges, symbols;
REVOKE SELECT (notified_at), UPDATE (notified_at) ON build_tasks FROM app_system;
ALTER TABLE build_tasks DROP COLUMN notified_at;
