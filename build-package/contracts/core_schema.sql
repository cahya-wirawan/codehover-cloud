-- PostgreSQL 18 design reference: illustrative core subset, NOT a complete migration.
-- Executed against PostgreSQL 18.0 with deployment/init-db.sh roles; see VALIDATION_REPORT.md.
--
-- Roles (created by bootstrap, never by migrations):
--   codehover_ddl  migration/DDL role, owns tables (local bootstrap user; non-superuser in production)
--   app_runtime    API requests and task workers; always inside a workspace context
--   app_system     cross-tenant control loops (dispatcher, scheduler, reconciler, GC); scheduling columns only
--   app_resolver   NOLOGIN owner of SECURITY DEFINER lookups; may read id/workspace_id columns only
--
-- Transaction context, set with bound parameters (never string-formatted SET LOCAL):
--   SELECT set_config('app.workspace_id', $1, true), set_config('app.user_id', $2, true);
BEGIN;

CREATE SCHEMA app_private;
REVOKE ALL ON SCHEMA app_private FROM PUBLIC;
GRANT USAGE ON SCHEMA app_private TO app_runtime;

CREATE TABLE workspaces (
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 name varchar(80) NOT NULL,
 slug varchar(48) NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT now()
);
-- users/oauth_identities omitted from this subset; memberships.user_id references users(id) in the full migration.
CREATE TABLE memberships (
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id),
 user_id uuid NOT NULL,
 role text NOT NULL CHECK (role IN ('owner','admin','editor','viewer')),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,user_id), UNIQUE(workspace_id,id)
);
CREATE INDEX memberships_user_idx ON memberships(user_id);
CREATE TABLE projects (
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id),
 name varchar(80) NOT NULL, slug varchar(48) NOT NULL,
 config jsonb NOT NULL DEFAULT '{}'::jsonb,
 config_version integer NOT NULL DEFAULT 1,
 deleted_at timestamptz,
 UNIQUE(workspace_id,id), UNIQUE(workspace_id,slug)
);
CREATE TABLE builds (
 id uuid PRIMARY KEY DEFAULT uuidv7(),
 workspace_id uuid NOT NULL,
 project_id uuid NOT NULL,
 commit_sha text NOT NULL CHECK (commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
 config_digest text NOT NULL CHECK (config_digest ~ '^[0-9a-f]{64}$'),
 status text NOT NULL CHECK(status IN ('queued','running','ready','failed','cancelled','budget_exhausted')),
 cap_micro_usd bigint NOT NULL CHECK(cap_micro_usd >= 0),
 reserved_micro_usd bigint NOT NULL DEFAULT 0 CHECK(reserved_micro_usd >= 0),
 settled_micro_usd bigint NOT NULL DEFAULT 0 CHECK(settled_micro_usd >= 0),
 cancel_requested_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,id), UNIQUE(workspace_id,project_id,id),
 CHECK(reserved_micro_usd + settled_micro_usd <= cap_micro_usd),
 FOREIGN KEY(workspace_id,project_id) REFERENCES projects(workspace_id,id)
);
CREATE UNIQUE INDEX one_active_build_per_project ON builds(project_id)
 WHERE status IN ('queued','running');
CREATE TABLE build_tasks (
 id uuid PRIMARY KEY DEFAULT uuidv7(), workspace_id uuid NOT NULL,
 build_id uuid NOT NULL, stage text NOT NULL, variant text NOT NULL DEFAULT 'en-standard',
 work_key text NOT NULL,
 state text NOT NULL DEFAULT 'pending'
   CHECK(state IN ('pending','leased','succeeded','skipped','retry_wait','failed','cancelled')),
 lease_generation integer NOT NULL DEFAULT 0,
 lease_until timestamptz, heartbeat_at timestamptz, next_attempt_at timestamptz,
 attempt_count integer NOT NULL DEFAULT 0,
 output_key text, error_code text,
 UNIQUE(build_id,stage,variant,work_key), UNIQUE(workspace_id,id),
 FOREIGN KEY(workspace_id,build_id) REFERENCES builds(workspace_id,id)
);
CREATE INDEX task_due_idx ON build_tasks(state,next_attempt_at);
CREATE TABLE publications (
 id uuid PRIMARY KEY DEFAULT uuidv7(), workspace_id uuid NOT NULL,
 project_id uuid NOT NULL UNIQUE, build_id uuid NOT NULL,
 visibility text NOT NULL CHECK(visibility IN ('private','public')),
 public_slug text UNIQUE CHECK (public_slug ~ '^[a-z0-9][a-z0-9-]{2,62}$'),
 version integer NOT NULL DEFAULT 1,
 suspended_at timestamptz,
 CHECK (visibility = 'private' OR public_slug IS NOT NULL),
 FOREIGN KEY(workspace_id,project_id,build_id) REFERENCES builds(workspace_id,project_id,id)
);
CREATE TABLE outbox (
 id uuid PRIMARY KEY DEFAULT uuidv7(), workspace_id uuid,
 aggregate_id uuid NOT NULL, event_type text NOT NULL, payload jsonb NOT NULL,
 available_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
 dispatch_attempts integer NOT NULL DEFAULT 0
);
CREATE INDEX outbox_due_idx ON outbox(available_at) WHERE published_at IS NULL;
CREATE TABLE usage_accounts (
 id uuid PRIMARY KEY DEFAULT uuidv7(), workspace_id uuid NOT NULL REFERENCES workspaces(id),
 period_start timestamptz NOT NULL, period_end timestamptz NOT NULL,
 allowance_micro_usd bigint NOT NULL CHECK(allowance_micro_usd >= 0),
 reserved_micro_usd bigint NOT NULL DEFAULT 0 CHECK(reserved_micro_usd >= 0),
 settled_micro_usd bigint NOT NULL DEFAULT 0 CHECK(settled_micro_usd >= 0),
 UNIQUE(workspace_id,id), UNIQUE(workspace_id,period_start),
 CHECK(period_end > period_start),
 CHECK(reserved_micro_usd + settled_micro_usd <= allowance_micro_usd)
);
CREATE TABLE usage_entries (
 id uuid PRIMARY KEY DEFAULT uuidv7(), workspace_id uuid NOT NULL,
 account_id uuid NOT NULL, idempotency_key text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('reserve','settle','release','adjustment','reversal')),
 amount_micro_usd bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,idempotency_key),
 FOREIGN KEY(workspace_id,account_id) REFERENCES usage_accounts(workspace_id,id)
);

-- Tenant policy: apply to every tenant entity. A missing setting yields NULL and denies all rows.
-- The runtime role must not own tables; FORCE also subjects the owner to policies.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['projects','builds','build_tasks','publications','outbox',
                           'usage_accounts','usage_entries','memberships'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($p$CREATE POLICY tenant ON %I TO app_runtime
      USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
      WITH CHECK (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)$p$, t);
  END LOOP;
END $$;

-- User-scoped reads: a signed-in user lists their own memberships/workspaces across tenants
-- (GET /workspaces, workspace switcher). Writes still require the workspace context.
CREATE POLICY own_memberships ON memberships FOR SELECT TO app_runtime
 USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_or_member ON workspaces FOR SELECT TO app_runtime
 USING (id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
        OR id IN (SELECT m.workspace_id FROM memberships m
                  WHERE m.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid));
CREATE POLICY tenant_write ON workspaces FOR UPDATE TO app_runtime
 USING (id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
 WITH CHECK (id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);

-- Lookups that run before a workspace context exists. Owned by app_resolver, which reads only
-- id/workspace_id/lifecycle columns through role-scoped policies (no BYPASSRLS anywhere).
GRANT SELECT (id, workspace_id, deleted_at) ON projects TO app_resolver;
GRANT SELECT (id, workspace_id) ON builds TO app_resolver;
GRANT SELECT (id, workspace_id, project_id, build_id, visibility, public_slug, suspended_at) ON publications TO app_resolver;
GRANT INSERT ON workspaces, memberships TO app_resolver;
GRANT SELECT (id) ON workspaces TO app_resolver;
CREATE POLICY resolver ON projects FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver ON builds FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver ON publications FOR SELECT TO app_resolver USING (suspended_at IS NULL);
CREATE POLICY resolver_create ON workspaces FOR INSERT TO app_resolver WITH CHECK (true);
CREATE POLICY resolver_select ON workspaces FOR SELECT TO app_resolver USING (true);
CREATE POLICY resolver_create ON memberships FOR INSERT TO app_resolver WITH CHECK (role = 'owner');

-- Flat routes (/projects/{pid}, /builds/{bid}, ...): resolve the owning workspace, then the
-- application checks membership/role and sets app.workspace_id. Unknown kind or id -> NULL -> 404.
CREATE FUNCTION app_private.resource_workspace(kind text, resource_id uuid) RETURNS uuid
 LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  RETURN CASE kind
    WHEN 'project' THEN (SELECT p.workspace_id FROM public.projects p WHERE p.id = resource_id AND p.deleted_at IS NULL)
    WHEN 'build'   THEN (SELECT b.workspace_id FROM public.builds b WHERE b.id = resource_id)
    ELSE NULL END;
END $$;
-- Published site resolver: exposes only what delivery needs, never unpublished build IDs to callers.
CREATE FUNCTION app_private.resolve_publication(slug text)
 RETURNS TABLE(workspace_id uuid, project_id uuid, build_id uuid, visibility text)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT p.workspace_id, p.project_id, p.build_id, p.visibility
  FROM public.publications p WHERE p.public_slug = slug AND p.suspended_at IS NULL
$$;
-- Workspace creation needs the new id before any context exists; creator comes from app.user_id.
CREATE FUNCTION app_private.create_workspace(ws_name text, ws_slug text) RETURNS uuid
 LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE uid uuid := NULLIF(current_setting('app.user_id', true), '')::uuid; wid uuid;
BEGIN
  IF uid IS NULL THEN RAISE EXCEPTION 'app.user_id not set' USING ERRCODE = '42501'; END IF;
  INSERT INTO public.workspaces(name, slug) VALUES (ws_name, ws_slug) RETURNING id INTO wid;
  INSERT INTO public.memberships(workspace_id, user_id, role) VALUES (wid, uid, 'owner');
  RETURN wid;
END $$;
ALTER FUNCTION app_private.resource_workspace(text, uuid) OWNER TO app_resolver;
ALTER FUNCTION app_private.resolve_publication(text) OWNER TO app_resolver;
ALTER FUNCTION app_private.create_workspace(text, text) OWNER TO app_resolver;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA app_private FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app_private TO app_runtime;

-- Cross-tenant control loops: dispatcher (outbox), scheduler/reconciler (due tasks, lease expiry).
-- Column grants keep source, prose, payload internals and ledgers out of reach.
GRANT SELECT (id, workspace_id, aggregate_id, event_type, payload, available_at, published_at, dispatch_attempts),
      UPDATE (published_at, dispatch_attempts, available_at), INSERT ON outbox TO app_system;
GRANT SELECT (id, workspace_id, build_id, stage, state, lease_generation, lease_until, heartbeat_at, next_attempt_at, attempt_count),
      UPDATE (state, next_attempt_at) ON build_tasks TO app_system;
CREATE POLICY system_loop ON outbox TO app_system USING (true) WITH CHECK (true);
CREATE POLICY system_loop ON build_tasks TO app_system USING (true) WITH CHECK (true);
-- Append-only ledger for the runtime role (local bootstrap grants broad default privileges).
REVOKE UPDATE, DELETE ON usage_entries FROM app_runtime;
-- Remaining entity/RLS migrations follow the complete inventory in 05_DATA_MODEL.md.
COMMIT;
