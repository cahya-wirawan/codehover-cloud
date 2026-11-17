-- Reverse of 0002_tenant_core.sql. Functions first, then tables in dependency order.
DROP FUNCTION app_private.oauth_transaction_consume(bytea, text);
DROP FUNCTION app_private.oauth_transaction_create(bytea, text, text, bytea, bytea, text, uuid, uuid, interval);
DROP FUNCTION app_private.session_by_token(bytea);
DROP FUNCTION app_private.create_user_with_identity(text, text, text, text, text);
DROP FUNCTION app_private.identity_user(text, text, text);
DROP FUNCTION app_private.create_workspace(text, text);
DROP FUNCTION app_private.resource_workspace(text, uuid);
DROP TABLE idempotency_keys, audit_events, usage_entries, usage_accounts, outbox, artifacts, build_events,
  build_tasks, builds, estimates, source_files, snapshots, projects, repositories, repository_connections,
  invitations, memberships, workspaces, oauth_transactions, sessions, oauth_identities, users;
