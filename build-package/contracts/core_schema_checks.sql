-- Executable checks for contracts/core_schema.sql. Run as the bootstrap/DDL user after the schema:
--   psql -v ON_ERROR_STOP=1 -f core_schema.sql -f core_schema_checks.sql
-- Everything runs in one transaction and is rolled back. Each failed assertion aborts the script.
-- These are the seeds of the CI tenancy suite (13_TEST_PLAN.md "DB integration"), not a replacement.
BEGIN;
-- Fixture: two workspaces, one project/build each, created through the owner path.
CREATE TEMP TABLE ids(k text PRIMARY KEY, v uuid) ON COMMIT DROP;
GRANT ALL ON ids TO PUBLIC;
INSERT INTO ids VALUES ('alice', uuidv7()), ('bob', uuidv7());
INSERT INTO workspaces(name, slug) VALUES ('A','ws-a'), ('B','ws-b');
INSERT INTO ids SELECT 'wa', id FROM workspaces WHERE slug='ws-a';
INSERT INTO ids SELECT 'wb', id FROM workspaces WHERE slug='ws-b';
INSERT INTO memberships(workspace_id,user_id,role)
 SELECT (SELECT v FROM ids WHERE k='wa'), (SELECT v FROM ids WHERE k='alice'), 'owner' UNION ALL
 SELECT (SELECT v FROM ids WHERE k='wb'), (SELECT v FROM ids WHERE k='bob'), 'owner';
INSERT INTO projects(workspace_id,name,slug) SELECT v,'P','p' FROM ids WHERE k IN ('wa','wb');
INSERT INTO builds(workspace_id,project_id,commit_sha,config_digest,status,cap_micro_usd)
 SELECT workspace_id, id, repeat('a',40), repeat('b',64), 'ready', 1000 FROM projects;
INSERT INTO build_tasks(workspace_id,build_id,stage,work_key,state,next_attempt_at)
 SELECT workspace_id, id, 'explain', 'f1', 'retry_wait', now() - interval '1 minute' FROM builds;
INSERT INTO outbox(workspace_id,aggregate_id,event_type,payload)
 SELECT workspace_id, id, 'task.ready', '{}'::jsonb FROM build_tasks;

-- C01 UUIDv7 server default.
DO $$ BEGIN ASSERT (SELECT bool_and(uuid_extract_version(id) = 7) FROM builds), 'C01 uuid v7'; END $$;

-- C02 runtime without context sees nothing.
SET LOCAL ROLE app_runtime;
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM projects) = 0, 'C02 projects visible without context';
  ASSERT (SELECT count(*) FROM build_tasks) = 0, 'C02 tasks visible without context';
  ASSERT (SELECT count(*) FROM workspaces) = 0, 'C02 workspaces visible without context';
END $$;

-- C03 workspace context limits rows to that tenant; cross-tenant insert is rejected.
SELECT set_config('app.workspace_id', (SELECT v FROM ids WHERE k='wa')::text, true);
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM projects) = 1, 'C03 expected exactly own project';
  ASSERT (SELECT bool_and(workspace_id = (SELECT v FROM ids WHERE k='wa')) FROM builds), 'C03 foreign build visible';
  BEGIN
    INSERT INTO projects(workspace_id,name,slug) VALUES ((SELECT v FROM ids WHERE k='wb'),'X','x');
    RAISE EXCEPTION 'C03 cross-tenant insert succeeded';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;

-- C04 user-scoped listing: alice sees her workspace, not bob's, without a workspace context.
SELECT set_config('app.workspace_id', '', true), set_config('app.user_id', (SELECT v FROM ids WHERE k='alice')::text, true);
DO $$ BEGIN
  ASSERT (SELECT array_agg(slug) FROM workspaces) = ARRAY['ws-a']::varchar[], 'C04 workspace list wrong';
  ASSERT (SELECT count(*) FROM memberships) = 1, 'C04 membership list wrong';
  ASSERT (SELECT count(*) FROM projects) = 0, 'C04 user scope must not expose projects';
END $$;

-- C05 resolver returns the owning workspace of a flat-route resource the runtime cannot see yet;
-- the application then checks membership/role before setting app.workspace_id.
RESET ROLE;
CREATE TEMP TABLE probe ON COMMIT DROP AS SELECT id AS project_id, workspace_id FROM projects;
GRANT SELECT ON probe TO PUBLIC;
SET LOCAL ROLE app_runtime;
DO $$ BEGIN
  ASSERT (SELECT bool_and(app_private.resource_workspace('project', project_id) = workspace_id) FROM probe), 'C05 resolver';
  ASSERT app_private.resource_workspace('project', uuidv7()) IS NULL, 'C05 unknown id must be NULL';
  ASSERT app_private.resource_workspace('symbol', (SELECT project_id FROM probe LIMIT 1)) IS NULL, 'C05 unknown kind';
END $$;

-- C06 create_workspace requires app.user_id and creates the owner membership atomically.
DO $$ DECLARE wid uuid; BEGIN
  wid := app_private.create_workspace('C', 'ws-c');
  ASSERT (SELECT count(*) FROM workspaces WHERE id = wid) = 1, 'C06 new workspace not visible to creator';
  PERFORM set_config('app.user_id', '', true);
  BEGIN PERFORM app_private.create_workspace('D', 'ws-d'); RAISE EXCEPTION 'C06 anonymous create';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;

-- C07 append-only ledger for runtime.
SELECT set_config('app.workspace_id', (SELECT v FROM ids WHERE k='wa')::text, true);
DO $$ BEGIN
  BEGIN UPDATE usage_entries SET amount_micro_usd = 0; RAISE EXCEPTION 'C07 ledger update allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET ROLE;

-- C08 system loop role sees due work across tenants, but no tenant content.
SET LOCAL ROLE app_system;
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM build_tasks WHERE state = 'retry_wait' AND next_attempt_at <= now()) = 2, 'C08 due tasks';
  ASSERT (SELECT count(*) FROM (SELECT id FROM outbox WHERE published_at IS NULL FOR UPDATE SKIP LOCKED) o) = 2, 'C08 outbox claim';
  BEGIN PERFORM 1 FROM projects; RAISE EXCEPTION 'C08 projects readable by system';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM output_key FROM build_tasks; RAISE EXCEPTION 'C08 task output readable by system';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET ROLE;

-- C09 integrity: one active build per project, publication must reference same-project build,
-- public visibility needs a slug, budget counters cannot exceed the cap.
DO $$ DECLARE pa uuid; wa uuid; bb uuid; BEGIN
  SELECT v INTO wa FROM ids WHERE k='wa';
  SELECT id INTO pa FROM projects WHERE workspace_id = wa;
  SELECT id INTO bb FROM builds WHERE workspace_id <> wa LIMIT 1;
  INSERT INTO builds(workspace_id,project_id,commit_sha,config_digest,status,cap_micro_usd)
    VALUES (wa, pa, repeat('c',40), repeat('d',64), 'queued', 10);
  BEGIN
    INSERT INTO builds(workspace_id,project_id,commit_sha,config_digest,status,cap_micro_usd)
      VALUES (wa, pa, repeat('e',40), repeat('f',64), 'running', 10);
    RAISE EXCEPTION 'C09 second active build';
  EXCEPTION WHEN unique_violation THEN NULL; END;
  BEGIN
    INSERT INTO publications(workspace_id,project_id,build_id,visibility) VALUES (wa, pa, bb, 'private');
    RAISE EXCEPTION 'C09 cross-project publication';
  EXCEPTION WHEN foreign_key_violation THEN NULL; END;
  BEGIN
    INSERT INTO publications(workspace_id,project_id,build_id,visibility)
      SELECT wa, pa, id, 'public' FROM builds WHERE project_id = pa AND status = 'ready';
    RAISE EXCEPTION 'C09 public without slug';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    UPDATE builds SET reserved_micro_usd = 9, settled_micro_usd = 2 WHERE project_id = pa AND status = 'queued';
    RAISE EXCEPTION 'C09 overspend';
  EXCEPTION WHEN check_violation THEN NULL; END;
END $$;

SELECT 'core_schema_checks: all assertions passed' AS result;
ROLLBACK;
