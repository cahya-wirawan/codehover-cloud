-- 0002 tenant core (backlog T02). Implements build-package/05_DATA_MODEL.md for M1, following the
-- executed reference in build-package/contracts/core_schema.sql. Runs as the DDL role; roles exist
-- already (deployment/init-db.sh or the production bootstrap).
--
-- Context, always set with bound parameters:
--   SELECT set_config('app.workspace_id', $1, true), set_config('app.user_id', $2, true);

-- ---------------------------------------------------------------- identity (not tenant-scoped)
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  display_name varchar(120) NOT NULL,
  primary_email text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled', 'deleted')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE TABLE oauth_identities (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  user_id uuid NOT NULL REFERENCES users(id),
  provider text NOT NULL CHECK (provider IN ('google', 'github')),
  issuer text NOT NULL,
  subject text NOT NULL,
  verified_email text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, issuer, subject)
);
CREATE INDEX ix_oauth_identities_user_id ON oauth_identities(user_id);

CREATE TABLE sessions (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  user_id uuid NOT NULL REFERENCES users(id),
  token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
  csrf_hash bytea NOT NULL CHECK (octet_length(csrf_hash) = 32),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  reauthenticated_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  CHECK (expires_at > created_at)
);
CREATE INDEX ix_sessions_user_id ON sessions(user_id);
CREATE INDEX ix_sessions_expires_at ON sessions(expires_at);

CREATE TABLE oauth_transactions (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  state_hash bytea NOT NULL UNIQUE CHECK (octet_length(state_hash) = 32),
  provider text NOT NULL CHECK (provider IN ('google', 'github', 'github_app')),
  purpose text NOT NULL CHECK (purpose IN ('login', 'link', 'install')),
  nonce_hash bytea CHECK (nonce_hash IS NULL OR octet_length(nonce_hash) = 32),
  encrypted_pkce_verifier bytea,
  -- Strict allowlist (no whitespace/control chars: browsers strip tabs/newlines, turning "/\t/x" into "//x"),
  -- no "//" anywhere and no dot segments. The application applies the same rule before redirecting.
  return_path text NOT NULL DEFAULT '/' CHECK (
    char_length(return_path) <= 512
    AND return_path ~ '^/[A-Za-z0-9._~!$&''()*+,;=:@%/?#-]*$'
    AND return_path !~ '//'
    AND return_path !~ '(^|/)\.\.?(/|\?|#|$)'
  ),
  initiating_session_id uuid REFERENCES sessions(id),
  workspace_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  CHECK (purpose = 'login' OR initiating_session_id IS NOT NULL)
);
CREATE INDEX ix_oauth_transactions_expires_at ON oauth_transactions(expires_at);

-- ---------------------------------------------------------------- workspaces
CREATE TABLE workspaces (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  name varchar(80) NOT NULL CHECK (char_length(btrim(name)) >= 2),
  slug varchar(48) NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$'),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleting')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE TABLE memberships (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  user_id uuid NOT NULL REFERENCES users(id),
  role text NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'viewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, user_id),
  UNIQUE (workspace_id, id)
);
CREATE INDEX ix_memberships_user_id ON memberships(user_id);

CREATE TABLE invitations (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  email_normalized text NOT NULL CHECK (email_normalized = lower(email_normalized) AND email_normalized LIKE '%_@_%'),
  role text NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'viewer')),
  token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
  invited_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  accepted_at timestamptz,
  accepted_by uuid REFERENCES users(id),
  revoked_at timestamptz,
  UNIQUE (workspace_id, id)
);
CREATE UNIQUE INDEX uq_invitations_live_email ON invitations(workspace_id, email_normalized)
  WHERE accepted_at IS NULL AND revoked_at IS NULL;

-- ---------------------------------------------------------------- GitHub
CREATE TABLE repository_connections (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  installation_id bigint NOT NULL CHECK (installation_id > 0),
  github_account_id bigint NOT NULL CHECK (github_account_id > 0),
  github_account_login text NOT NULL,
  connected_by uuid NOT NULL REFERENCES users(id),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'revoked')),
  last_verified_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, installation_id),
  UNIQUE (workspace_id, id)
);
CREATE INDEX ix_repository_connections_installation ON repository_connections(installation_id) WHERE status <> 'revoked';

CREATE TABLE repositories (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  connection_id uuid NOT NULL,
  provider_repo_id bigint NOT NULL CHECK (provider_repo_id > 0),
  owner text NOT NULL,
  name text NOT NULL,
  visibility text NOT NULL CHECK (visibility IN ('public', 'private', 'internal')),
  default_branch text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, provider_repo_id),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, connection_id) REFERENCES repository_connections(workspace_id, id)
);

-- ---------------------------------------------------------------- projects and snapshots
CREATE TABLE projects (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  repository_id uuid NOT NULL,
  name varchar(80) NOT NULL CHECK (char_length(btrim(name)) >= 1),
  slug varchar(48) NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,46}[a-z0-9]$|^[a-z0-9]$'),
  tracked_ref text NOT NULL,
  config jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(config) = 'object'),
  config_version integer NOT NULL DEFAULT 1 CHECK (config_version >= 1),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, slug),
  FOREIGN KEY (workspace_id, repository_id) REFERENCES repositories(workspace_id, id)
);

CREATE TABLE snapshots (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  commit_sha text NOT NULL CHECK (commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
  observed_ref text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'acquiring', 'scanned', 'rejected', 'failed')),
  rejection_code text,
  archive_key text,
  digest text CHECK (digest IS NULL OR digest ~ '^[0-9a-f]{64}$'),
  source_bytes bigint CHECK (source_bytes IS NULL OR source_bytes >= 0),
  file_count integer CHECK (file_count IS NULL OR file_count >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, commit_sha),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, project_id, id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id)
);

CREATE TABLE source_files (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  snapshot_id uuid NOT NULL,
  path text NOT NULL CHECK (path !~ '(^/|\\|(^|/)\.\.(/|$)|//)' AND char_length(path) BETWEEN 1 AND 1024),
  language text,
  sha256 text CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
  blob_key text,
  line_count integer CHECK (line_count IS NULL OR line_count >= 0),
  byte_count bigint NOT NULL CHECK (byte_count >= 0),
  scan_status text NOT NULL
    CHECK (scan_status IN ('selected', 'excluded', 'binary', 'oversize', 'unsupported', 'blocked')),
  scan_reason text,
  UNIQUE (snapshot_id, path),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, snapshot_id) REFERENCES snapshots(workspace_id, id)
);

-- ---------------------------------------------------------------- estimates, builds, tasks, events
CREATE TABLE estimates (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  snapshot_id uuid NOT NULL,
  config jsonb NOT NULL CHECK (jsonb_typeof(config) = 'object'),
  config_digest text NOT NULL CHECK (config_digest ~ '^[0-9a-f]{64}$'),
  price_version text NOT NULL,
  low_micro_usd bigint NOT NULL CHECK (low_micro_usd >= 0),
  high_micro_usd bigint NOT NULL,
  token_estimate jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  CHECK (high_micro_usd >= low_micro_usd),
  CHECK (expires_at > created_at),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, project_id, id),
  FOREIGN KEY (workspace_id, project_id, snapshot_id) REFERENCES snapshots(workspace_id, project_id, id)
);

CREATE TABLE builds (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  snapshot_id uuid NOT NULL,
  estimate_id uuid,
  retry_of_id uuid,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'ready', 'failed', 'cancelled', 'budget_exhausted')),
  config jsonb NOT NULL CHECK (jsonb_typeof(config) = 'object'),
  config_digest text NOT NULL CHECK (config_digest ~ '^[0-9a-f]{64}$'),
  engine_version text NOT NULL,
  prompt_version text NOT NULL,
  cap_micro_usd bigint NOT NULL CHECK (cap_micro_usd >= 0),
  reserved_micro_usd bigint NOT NULL DEFAULT 0 CHECK (reserved_micro_usd >= 0),
  settled_micro_usd bigint NOT NULL DEFAULT 0 CHECK (settled_micro_usd >= 0),
  cancel_requested_at timestamptz,
  manifest_key text,
  created_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CHECK (reserved_micro_usd + settled_micro_usd <= cap_micro_usd),
  CHECK ((status IN ('queued', 'running')) = (completed_at IS NULL)),
  CHECK (retry_of_id IS NULL OR retry_of_id <> id),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, project_id, id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES projects(workspace_id, id),
  FOREIGN KEY (workspace_id, project_id, snapshot_id) REFERENCES snapshots(workspace_id, project_id, id),
  FOREIGN KEY (workspace_id, project_id, estimate_id) REFERENCES estimates(workspace_id, project_id, id),
  FOREIGN KEY (workspace_id, project_id, retry_of_id) REFERENCES builds(workspace_id, project_id, id)
);
CREATE UNIQUE INDEX uq_builds_one_active_per_project ON builds(project_id) WHERE status IN ('queued', 'running');
CREATE INDEX ix_builds_project_status ON builds(project_id, status);

CREATE TABLE build_tasks (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  stage text NOT NULL,
  variant text NOT NULL DEFAULT 'en-standard',
  work_key text NOT NULL,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'leased', 'succeeded', 'skipped', 'retry_wait', 'failed', 'cancelled')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
  lease_until timestamptz,
  heartbeat_at timestamptz,
  next_attempt_at timestamptz,
  output_key text,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((state = 'leased') = (lease_until IS NOT NULL)),
  UNIQUE (build_id, stage, variant, work_key),
  UNIQUE (workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id)
);
CREATE INDEX ix_build_tasks_due ON build_tasks(state, next_attempt_at);
CREATE INDEX ix_build_tasks_lease ON build_tasks(lease_until) WHERE state = 'leased';

CREATE TABLE build_events (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  sequence bigint NOT NULL CHECK (sequence >= 1),
  event_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (build_id, sequence),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id)
);

CREATE TABLE artifacts (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  build_id uuid NOT NULL,
  kind text NOT NULL,
  variant text NOT NULL DEFAULT 'en-standard',
  object_key text NOT NULL,
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  byte_count bigint NOT NULL CHECK (byte_count >= 0),
  content_type text NOT NULL,
  status text NOT NULL DEFAULT 'stored' CHECK (status IN ('stored', 'deleted')),
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (build_id, kind, variant),
  -- Own workspace prefix, conservative charset, no empty or dot segments (some S3-compatible stores and
  -- filesystem adapters normalize paths, which would let "../" escape the prefix).
  CHECK (object_key LIKE 'workspaces/' || workspace_id::text || '/%'
         AND object_key ~ '^workspaces/[0-9a-f-]{36}/[A-Za-z0-9._/-]+$'
         AND object_key !~ '(^|/)\.\.?(/|$)|//'),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id)
);

CREATE TABLE outbox (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid REFERENCES workspaces(id),
  aggregate_id uuid NOT NULL,
  event_type text NOT NULL,
  payload jsonb NOT NULL,
  available_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  dispatch_attempts integer NOT NULL DEFAULT 0 CHECK (dispatch_attempts >= 0),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbox_due ON outbox(available_at) WHERE published_at IS NULL;

-- ---------------------------------------------------------------- usage ledger
CREATE TABLE usage_accounts (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  period_start timestamptz NOT NULL,
  period_end timestamptz NOT NULL,
  allowance_micro_usd bigint NOT NULL CHECK (allowance_micro_usd >= 0),
  reserved_micro_usd bigint NOT NULL DEFAULT 0 CHECK (reserved_micro_usd >= 0),
  settled_micro_usd bigint NOT NULL DEFAULT 0 CHECK (settled_micro_usd >= 0),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, period_start),
  CHECK (period_end > period_start),
  CHECK (reserved_micro_usd + settled_micro_usd <= allowance_micro_usd)
);

CREATE TABLE usage_entries (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid NOT NULL,
  account_id uuid NOT NULL,
  build_id uuid,
  kind text NOT NULL CHECK (kind IN ('reserve', 'settle', 'release', 'adjustment', 'reversal')),
  amount_micro_usd bigint NOT NULL,
  idempotency_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, idempotency_key),
  FOREIGN KEY (workspace_id, account_id) REFERENCES usage_accounts(workspace_id, id),
  FOREIGN KEY (workspace_id, build_id) REFERENCES builds(workspace_id, id)
);

-- ---------------------------------------------------------------- audit and idempotency
CREATE TABLE audit_events (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  workspace_id uuid REFERENCES workspaces(id),
  actor_user_id uuid REFERENCES users(id),
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id uuid,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  request_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_events_workspace_created ON audit_events(workspace_id, created_at);

-- 06 "Shared behavior": key scoped by principal/route, body digest stored 24h.
CREATE TABLE idempotency_keys (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  user_id uuid NOT NULL REFERENCES users(id),
  workspace_id uuid REFERENCES workspaces(id),
  route text NOT NULL,
  key text NOT NULL CHECK (char_length(key) BETWEEN 16 AND 128),
  request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
  response_status smallint,
  response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  UNIQUE (user_id, route, key)
);
CREATE INDEX ix_idempotency_keys_expires_at ON idempotency_keys(expires_at);

-- ================================================================ privileges
-- Explicit grants (production DDL roles have no broad default privileges).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime;
REVOKE ALL ON alembic_version FROM app_runtime;
GRANT SELECT ON alembic_version TO app_runtime;
-- Pre-authentication state is reachable only through the lookup functions below.
REVOKE ALL ON oauth_transactions FROM app_runtime;
-- Append-only for the runtime role; corrections are reversal entries (05).
REVOKE UPDATE, DELETE ON usage_entries, audit_events FROM app_runtime;
-- Users are created only through app_private.create_user_with_identity; identities may be linked/unlinked
-- by their own user (RLS `own`), and the unique subject prevents claiming someone else's identity.
REVOKE INSERT, DELETE ON users FROM app_runtime;

-- ================================================================ row-level security
DO $$
DECLARE t text;
BEGIN
  -- Plain tenant tables: visible and writable only inside their workspace context.
  FOREACH t IN ARRAY ARRAY[
    'memberships', 'invitations', 'repository_connections', 'repositories', 'projects', 'snapshots',
    'source_files', 'estimates', 'builds', 'build_tasks', 'build_events', 'artifacts', 'outbox',
    'usage_accounts', 'usage_entries'
  ] LOOP
    EXECUTE format('CREATE POLICY tenant ON %I TO app_runtime
      USING (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)
      WITH CHECK (workspace_id = NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
  END LOOP;
  -- Every table, tenant or identity, forces RLS so even the owner obeys policies.
  FOREACH t IN ARRAY ARRAY[
    'users', 'oauth_identities', 'sessions', 'oauth_transactions', 'workspaces', 'memberships',
    'invitations', 'repository_connections', 'repositories', 'projects', 'snapshots', 'source_files',
    'estimates', 'builds', 'build_tasks', 'build_events', 'artifacts', 'outbox', 'usage_accounts',
    'usage_entries', 'audit_events', 'idempotency_keys'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;

-- Current user: own row, plus users who share the current workspace (member lists).
CREATE POLICY self_or_coworker ON users FOR SELECT TO app_runtime
  USING (id = NULLIF(current_setting('app.user_id', true), '')::uuid
         OR id IN (SELECT m.user_id FROM memberships m
                   WHERE m.workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid));
CREATE POLICY self_update ON users FOR UPDATE TO app_runtime
  USING (id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY own ON oauth_identities TO app_runtime
  USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY own ON sessions TO app_runtime
  USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY own ON idempotency_keys TO app_runtime
  USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
              AND (workspace_id IS NULL
                   OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid));

-- Workspaces: the current workspace, or any workspace the user belongs to (switcher). Updates only in context.
CREATE POLICY tenant_or_member ON workspaces FOR SELECT TO app_runtime
  USING (id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
         OR id IN (SELECT m.workspace_id FROM memberships m
                   WHERE m.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid));
CREATE POLICY tenant_update ON workspaces FOR UPDATE TO app_runtime
  USING (id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
  WITH CHECK (id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY own_memberships ON memberships FOR SELECT TO app_runtime
  USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- Audit: tenant events readable in context; account-level events (workspace_id NULL) insert-only.
CREATE POLICY tenant_read ON audit_events FOR SELECT TO app_runtime
  USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY tenant_or_account_insert ON audit_events FOR INSERT TO app_runtime
  WITH CHECK (workspace_id IS NULL
              OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

-- ================================================================ control loops (app_system)
GRANT SELECT (id, workspace_id, aggregate_id, event_type, payload, available_at, published_at, dispatch_attempts),
      UPDATE (published_at, dispatch_attempts, available_at), INSERT ON outbox TO app_system;
GRANT SELECT (id, workspace_id, build_id, stage, variant, state, lease_generation, lease_until, heartbeat_at,
              next_attempt_at, attempt_count),
      UPDATE (state, next_attempt_at, lease_until, updated_at) ON build_tasks TO app_system;
CREATE POLICY system_loop ON outbox TO app_system USING (true) WITH CHECK (true);
CREATE POLICY system_loop ON build_tasks TO app_system USING (true) WITH CHECK (true);

-- ================================================================ lookups (owned by app_resolver)
CREATE POLICY resolver ON projects FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver ON builds FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_select ON workspaces FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_insert ON workspaces FOR INSERT TO app_resolver WITH CHECK (true);
CREATE POLICY resolver_insert ON memberships FOR INSERT TO app_resolver WITH CHECK (role = 'owner');
CREATE POLICY resolver_select ON users FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_insert ON users FOR INSERT TO app_resolver WITH CHECK (true);
CREATE POLICY resolver_select ON oauth_identities FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_insert ON oauth_identities FOR INSERT TO app_resolver WITH CHECK (true);
CREATE POLICY resolver_select ON sessions FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver ON oauth_transactions TO app_resolver USING (true) WITH CHECK (true);

GRANT SELECT (id, workspace_id, deleted_at) ON projects TO app_resolver;
GRANT SELECT (id, workspace_id) ON builds TO app_resolver;
GRANT SELECT (id), INSERT ON workspaces TO app_resolver;
GRANT INSERT ON memberships TO app_resolver;
GRANT SELECT (id, status), INSERT ON users TO app_resolver;
GRANT SELECT (user_id, provider, issuer, subject), INSERT ON oauth_identities TO app_resolver;
GRANT SELECT (id, user_id, token_hash, csrf_hash, created_at, expires_at, last_seen_at, reauthenticated_at,
              revoked_at) ON sessions TO app_resolver;
GRANT SELECT, INSERT, UPDATE (consumed_at) ON oauth_transactions TO app_resolver;

-- Flat routes: owning workspace of a resource; the API then checks membership (05).
CREATE FUNCTION app_private.resource_workspace(kind text, resource_id uuid) RETURNS uuid
  LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  RETURN CASE kind
    WHEN 'project' THEN (SELECT p.workspace_id FROM public.projects p WHERE p.id = resource_id AND p.deleted_at IS NULL)
    WHEN 'build' THEN (SELECT b.workspace_id FROM public.builds b WHERE b.id = resource_id)
    ELSE NULL END;
END $$;

-- Workspace creation with its owner membership; the creator is app.user_id.
CREATE FUNCTION app_private.create_workspace(ws_name text, ws_slug text) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid; wid uuid;
BEGIN
  IF uid IS NULL THEN RAISE EXCEPTION 'app.user_id not set' USING ERRCODE = '42501'; END IF;
  INSERT INTO public.workspaces(name, slug) VALUES (ws_name, ws_slug) RETURNING id INTO wid;
  INSERT INTO public.memberships(workspace_id, user_id, role) VALUES (wid, uid, 'owner');
  RETURN wid;
END $$;

-- Sign-in: which user owns this provider subject (identity, never email, is the authority).
CREATE FUNCTION app_private.identity_user(p_provider text, p_issuer text, p_subject text) RETURNS uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.user_id FROM public.oauth_identities i JOIN public.users u ON u.id = i.user_id
  WHERE i.provider = p_provider AND i.issuer = p_issuer AND i.subject = p_subject AND u.status = 'active'
$$;

-- First sign-in: user and identity in one statement; unique subject makes races fail closed.
CREATE FUNCTION app_private.create_user_with_identity(
  p_provider text, p_issuer text, p_subject text, p_display_name text, p_verified_email text
) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE uid uuid;
BEGIN
  INSERT INTO public.users(display_name, primary_email) VALUES (p_display_name, p_verified_email) RETURNING id INTO uid;
  INSERT INTO public.oauth_identities(user_id, provider, issuer, subject, verified_email)
    VALUES (uid, p_provider, p_issuer, p_subject, p_verified_email);
  RETURN uid;
END $$;

-- Every authenticated request: the live session for a token digest (idle policy stays in the app).
CREATE FUNCTION app_private.session_by_token(p_token_hash bytea)
  RETURNS TABLE(session_id uuid, user_id uuid, csrf_hash bytea, expires_at timestamptz,
                last_seen_at timestamptz, reauthenticated_at timestamptz)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT s.id, s.user_id, s.csrf_hash, s.expires_at, s.last_seen_at, s.reauthenticated_at
  FROM public.sessions s JOIN public.users u ON u.id = s.user_id
  WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL AND s.expires_at > now() AND u.status = 'active'
$$;

-- OAuth state: create, then consume exactly once before expiry.
CREATE FUNCTION app_private.oauth_transaction_create(
  p_state_hash bytea, p_provider text, p_purpose text, p_nonce_hash bytea, p_encrypted_pkce bytea,
  p_return_path text, p_initiating_session uuid, p_workspace uuid, p_ttl interval
) RETURNS uuid
  LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE tid uuid;
BEGIN
  IF p_ttl <= interval '0' OR p_ttl > interval '10 minutes' THEN
    RAISE EXCEPTION 'OAuth transaction TTL must be within (0, 10 minutes]' USING ERRCODE = '22023';
  END IF;
  INSERT INTO public.oauth_transactions(state_hash, provider, purpose, nonce_hash, encrypted_pkce_verifier,
                                        return_path, initiating_session_id, workspace_id, expires_at)
    VALUES (p_state_hash, p_provider, p_purpose, p_nonce_hash, p_encrypted_pkce, p_return_path,
            p_initiating_session, p_workspace, now() + p_ttl)
    RETURNING id INTO tid;
  RETURN tid;
END $$;

CREATE FUNCTION app_private.oauth_transaction_consume(p_state_hash bytea, p_provider text)
  RETURNS TABLE(purpose text, nonce_hash bytea, encrypted_pkce_verifier bytea, return_path text,
                initiating_session_id uuid, workspace_id uuid)
  LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  UPDATE public.oauth_transactions t SET consumed_at = now()
  WHERE t.state_hash = p_state_hash AND t.provider = p_provider
    AND t.consumed_at IS NULL AND t.expires_at > now()
  RETURNING t.purpose, t.nonce_hash, t.encrypted_pkce_verifier, t.return_path, t.initiating_session_id, t.workspace_id
$$;

ALTER FUNCTION app_private.resource_workspace(text, uuid) OWNER TO app_resolver;
ALTER FUNCTION app_private.create_workspace(text, text) OWNER TO app_resolver;
ALTER FUNCTION app_private.identity_user(text, text, text) OWNER TO app_resolver;
ALTER FUNCTION app_private.create_user_with_identity(text, text, text, text, text) OWNER TO app_resolver;
ALTER FUNCTION app_private.session_by_token(bytea) OWNER TO app_resolver;
ALTER FUNCTION app_private.oauth_transaction_create(bytea, text, text, bytea, bytea, text, uuid, uuid, interval)
  OWNER TO app_resolver;
ALTER FUNCTION app_private.oauth_transaction_consume(bytea, text) OWNER TO app_resolver;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA app_private FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app_private TO app_runtime;
