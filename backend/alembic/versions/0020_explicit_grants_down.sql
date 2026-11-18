GRANT UPDATE ON search_documents TO app_runtime;
GRANT DELETE ON webhook_receipts TO app_runtime;
GRANT DELETE ON provider_attempts TO app_runtime;
GRANT UPDATE ON support_grants TO app_runtime;
REVOKE DELETE ON claim_checks, exports FROM app_runtime;
