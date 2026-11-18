DROP TRIGGER IF EXISTS publications_suspend_on_repository ON repositories;
DROP TRIGGER IF EXISTS publications_suspend_on_connection ON repository_connections;
DROP FUNCTION IF EXISTS app_private.suspend_publications();
REVOKE SELECT (id, connection_id) ON repositories FROM app_resolver;
REVOKE SELECT (id, repository_id) ON projects FROM app_resolver;
DROP POLICY IF EXISTS resolver_publication_lookup ON repositories;
DROP POLICY IF EXISTS resolver_publication_lookup ON projects;
DROP FUNCTION IF EXISTS app_private.resolve_publication(text);
DROP TABLE IF EXISTS publications;
