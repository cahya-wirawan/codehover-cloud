-- 0020 runtime privileges come from migrations alone (found by the M4 restore drill).

-- A database bootstrapped without deployment/init-db.sh's broad default privileges could not purge:
-- two privileges the application needs were only ever granted by those defaults. And the defaults
-- granted four the application must not have. After this migration, a database migrated without
-- any default privileges grants the runtime exactly what a bootstrapped one does
-- (tests/integration/test_database.py).
GRANT DELETE ON claim_checks, exports TO app_runtime;            -- purge (0015) and retention (0016)
REVOKE UPDATE ON support_grants FROM app_runtime;                 -- only revocation, never a longer grant
GRANT UPDATE (revoked_at) ON support_grants TO app_runtime;
REVOKE DELETE ON provider_attempts FROM app_runtime;              -- the ledger refers to attempts (11)
REVOKE DELETE ON webhook_receipts FROM app_runtime;               -- delivery deduplication record
REVOKE UPDATE ON search_documents FROM app_runtime;               -- rebuilt per build, never edited
