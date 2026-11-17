"""Tenancy, identity and role boundaries of migration 0002 (T02; 05 "Tenant context resolution").

Ports build-package/contracts/core_schema_checks.sql to the real schema and extends it to the identity,
session and OAuth-transaction paths. Each check names the guarantee it protects.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
import sqlalchemy as sa

from tests.integration.conftest import Db, World, digest, needs_db, needs_system

pytestmark = [pytest.mark.integration, needs_db]

DENIED = sa.exc.ProgrammingError  # 42501 insufficient_privilege, including RLS WITH CHECK failures
REJECTED = sa.exc.IntegrityError  # unique, check and foreign-key violations


def expect(db: Db, error: type[Exception], sql: str, **params: Any) -> None:
    """Run a statement that must fail, inside a savepoint so the test transaction stays usable."""
    savepoint = db.conn.begin_nested()
    with pytest.raises(error):
        db.run(sql, **params)
    savepoint.rollback()


# ---------------------------------------------------------------- tenant isolation
def test_no_context_sees_no_tenant_or_identity_rows(world: World, runtime: Db) -> None:
    for table in (
        "users",
        "workspaces",
        "memberships",
        "projects",
        "builds",
        "build_tasks",
        "outbox",
        "sessions",
    ):
        assert runtime.one(f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608 - fixed table names


def test_workspace_context_shows_only_that_tenant(world: World, runtime: Db) -> None:
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    assert {r[0] for r in runtime.rows("SELECT id FROM projects")} == {world.a.project}
    assert {r[0] for r in runtime.rows("SELECT id FROM builds")} == {world.a.build}
    assert runtime.one("SELECT count(*) FROM builds WHERE id = :b", b=world.b.build) == 0


def test_writes_into_another_tenant_are_rejected(world: World, runtime: Db) -> None:
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    expect(
        runtime,
        DENIED,
        "INSERT INTO projects(workspace_id, repository_id, name, slug, tracked_ref)"
        " VALUES (:w, :r, 'x', 'x', 'main')",
        w=world.b.workspace,
        r=world.b.repository,
    )
    # Updating a foreign row is a silent no-op: it is invisible, so nothing matches.
    runtime.run("UPDATE projects SET name = 'hijacked' WHERE id = :p", p=world.b.project)
    runtime.context(workspace=world.b.workspace, user=world.b.user)
    assert runtime.one("SELECT name FROM projects WHERE id = :p", p=world.b.project) == "Project"


def test_composite_foreign_keys_block_cross_tenant_references_even_for_the_owner(
    world: World, ddl: Db
) -> None:
    # The DDL role is not limited by policy here, so this proves the constraint itself.
    expect(
        ddl,
        REJECTED,
        "INSERT INTO snapshots(workspace_id, project_id, commit_sha, observed_ref)"
        " VALUES (:w, :p, :sha, 'main')",
        w=world.a.workspace,
        p=world.b.project,
        sha=secrets.token_hex(20),
    )


# ---------------------------------------------------------------- user-scoped reads
def test_user_scope_lists_own_workspaces_and_coworkers_only(world: World, runtime: Db) -> None:
    runtime.context(user=world.a.user)
    assert {r[0] for r in runtime.rows("SELECT id FROM workspaces")} == {world.a.workspace}
    assert {r[0] for r in runtime.rows("SELECT id FROM users")} == {world.a.user}
    assert runtime.one("SELECT count(*) FROM projects") == 0  # user scope never opens tenant data
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    assert runtime.one("SELECT count(*) FROM users WHERE id = :u", u=world.b.user) == 0


def test_users_cannot_be_created_directly(world: World, runtime: Db) -> None:
    runtime.context(user=world.a.user)
    expect(runtime, DENIED, "INSERT INTO users(display_name) VALUES ('mallory')")


# ---------------------------------------------------------------- resolver functions
def test_resource_workspace_resolves_flat_routes_without_exposing_rows(world: World, runtime: Db) -> None:
    resolve = "SELECT app_private.resource_workspace(:k, :id)"
    assert runtime.one(resolve, k="project", id=world.b.project) == world.b.workspace
    assert runtime.one(resolve, k="build", id=world.b.build) == world.b.workspace
    assert runtime.one(resolve, k="project", id=world.b.build) is None  # wrong kind
    assert runtime.one(resolve, k="symbol", id=world.b.project) is None  # unknown kind
    assert runtime.one("SELECT count(*) FROM projects WHERE id = :p", p=world.b.project) == 0


def test_create_workspace_requires_a_user_and_makes_them_owner(world: World, runtime: Db) -> None:
    expect(
        runtime, DENIED, "SELECT app_private.create_workspace('Nobody', :s)", s=f"n-{secrets.token_hex(4)}"
    )
    runtime.context(user=world.a.user)
    created = runtime.one("SELECT app_private.create_workspace('Second', :s)", s=f"s-{secrets.token_hex(4)}")
    runtime.context(workspace=created, user=world.a.user)
    role = runtime.one(
        "SELECT role FROM memberships WHERE workspace_id = :w AND user_id = :u", w=created, u=world.a.user
    )
    assert role == "owner"


# ---------------------------------------------------------------- identities and sessions
def test_identity_is_found_by_subject_and_subjects_cannot_be_claimed_twice(world: World, runtime: Db) -> None:
    runtime.context(user=world.a.user)  # identities are readable only by their own user
    subject = runtime.one("SELECT subject FROM oauth_identities WHERE user_id = :u", u=world.a.user)
    runtime.context()
    found = runtime.one("SELECT app_private.identity_user('github', 'https://github.com', :s)", s=subject)
    assert found == world.a.user
    expect(
        runtime,
        REJECTED,
        "SELECT app_private.create_user_with_identity('github', 'https://github.com', :s, 'Imposter', NULL)",
        s=subject,
    )


def test_sessions_resolve_by_token_digest_only_while_live(world: World, runtime: Db) -> None:
    live, revoked, expired = (secrets.token_urlsafe(32) for _ in range(3))
    insert = (
        "INSERT INTO sessions(user_id, token_hash, csrf_hash, created_at, expires_at, revoked_at)"
        " VALUES (:u, :t, :c, now() + CAST(:created AS interval), now() + CAST(:expires AS interval),"
        " CASE WHEN :revoked THEN now() END)"
    )
    runtime.context(user=world.a.user)
    for token, created, expires, is_revoked in (
        (live, "0 seconds", "30 days", False),
        (revoked, "0 seconds", "30 days", True),
        (expired, "-31 days", "-1 day", False),
    ):
        runtime.run(
            insert,
            u=world.a.user,
            t=digest(token),
            c=digest("csrf-" + token),
            created=created,
            expires=expires,
            revoked=is_revoked,
        )
    runtime.context()
    lookup = "SELECT user_id FROM app_private.session_by_token(:t)"
    assert [r[0] for r in runtime.rows(lookup, t=digest(live))] == [world.a.user]
    assert runtime.rows(lookup, t=digest(revoked)) == []
    assert runtime.rows(lookup, t=digest(expired)) == []
    # Sessions belong to their user: another user can neither see nor create them.
    runtime.context(user=world.b.user)
    assert runtime.one("SELECT count(*) FROM sessions WHERE user_id = :u", u=world.a.user) == 0
    expect(
        runtime,
        DENIED,
        "INSERT INTO sessions(user_id, token_hash, csrf_hash, expires_at)"
        " VALUES (:u, :t, :c, now() + interval '1 day')",
        u=world.a.user,
        t=digest(secrets.token_hex(8)),
        c=digest("c3"),
    )


# ---------------------------------------------------------------- OAuth transactions
def create_tx(db: Db, state: str, *, ttl: str = "10 minutes", purpose: str = "login", **kw: Any) -> None:
    db.run(
        "SELECT app_private.oauth_transaction_create(:st, 'google', :purpose, :nonce, NULL, :rp,"
        " :sess, NULL, CAST(:ttl AS interval))",
        st=digest(state),
        purpose=purpose,
        nonce=digest("n-" + state),
        rp=kw.get("return_path", "/"),
        sess=kw.get("session"),
        ttl=ttl,
    )


def test_oauth_state_is_single_use_and_bound_to_provider(world: World, runtime: Db) -> None:
    state = secrets.token_urlsafe(32)
    create_tx(runtime, state, return_path="/w/settings")
    consume = "SELECT return_path FROM app_private.oauth_transaction_consume(:st, :p)"
    assert runtime.rows(consume, st=digest(state), p="github") == []  # wrong provider
    assert runtime.rows(consume, st=digest(state), p="google")[0][0] == "/w/settings"
    assert runtime.rows(consume, st=digest(state), p="google") == []  # replay
    expect(runtime, DENIED, "SELECT count(*) FROM oauth_transactions")  # no direct access at all


def test_oauth_state_rules(world: World, runtime: Db) -> None:
    for ttl in ("11 minutes", "0 seconds", "-1 second"):
        expect(
            runtime,
            sa.exc.DataError,  # 22023 invalid_parameter_value
            "SELECT app_private.oauth_transaction_create(:st, 'google', 'login', NULL, NULL, '/', NULL, NULL,"
            " CAST(:ttl AS interval))",
            st=digest(secrets.token_hex(8)),
            ttl=ttl,
        )
    for good_path in ("/", "/w/settings?tab=members", "/p/0193/code#L10", "/a-b_c.d~e"):
        create_tx(runtime, secrets.token_hex(8), return_path=good_path)
    bad_paths = (
        "//evil.example",
        "https://evil.example",
        "/\\evil",
        "relative",
        "/\t/evil.example",
        "/\n/evil.example",
        "/\r\n//evil.example",
        "/ /evil",
        "/x//y",
        "/..",
        "/a/../b",
        "/./x",
        "/a/..?q",
        "/\u00e9vil",
    )
    for bad_path in bad_paths:
        savepoint = runtime.conn.begin_nested()
        with pytest.raises(REJECTED):
            create_tx(runtime, secrets.token_hex(8), return_path=bad_path)
        savepoint.rollback()
    savepoint = runtime.conn.begin_nested()  # linking requires an initiating session
    with pytest.raises(REJECTED):
        create_tx(runtime, secrets.token_hex(8), purpose="link")
    savepoint.rollback()


def test_expired_oauth_state_cannot_be_consumed(ddl: Db) -> None:
    state = secrets.token_urlsafe(32)
    create_tx(ddl, state)
    ddl.run(
        "UPDATE oauth_transactions SET expires_at = now() - interval '1 second' WHERE state_hash = :s",
        s=digest(state),
    )
    assert (
        ddl.rows("SELECT 1 FROM app_private.oauth_transaction_consume(:s, 'google')", s=digest(state)) == []
    )


# ---------------------------------------------------------------- append-only and audit
def test_ledger_and_audit_are_append_only(world: World, runtime: Db) -> None:
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    for table in ("usage_entries", "audit_events"):
        expect(runtime, DENIED, f"UPDATE {table} SET created_at = now()")  # noqa: S608 - fixed names
        expect(runtime, DENIED, f"DELETE FROM {table}")  # noqa: S608
    runtime.run(
        "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type)"
        " VALUES (:w, :u, 'project.created', 'project')",
        w=world.a.workspace,
        u=world.a.user,
    )
    runtime.run(
        "INSERT INTO audit_events(workspace_id, actor_user_id, action, resource_type)"
        " VALUES (NULL, :u, 'session.created', 'session')",
        u=world.a.user,
    )
    expect(
        runtime,
        DENIED,
        "INSERT INTO audit_events(workspace_id, action, resource_type) VALUES (:w, 'x', 'x')",
        w=world.b.workspace,
    )
    assert runtime.one("SELECT count(*) FROM audit_events WHERE workspace_id IS NULL") == 0


# ---------------------------------------------------------------- control-loop role
@needs_system
def test_system_role_sees_due_work_across_tenants_but_no_content(world: World, system: Db) -> None:
    ids = (world.a.task, world.b.task)
    due = system.rows(
        "SELECT id FROM build_tasks WHERE id IN (:a, :b) AND state = 'retry_wait' AND"
        " next_attempt_at <= now()",
        a=ids[0],
        b=ids[1],
    )
    assert {r[0] for r in due} == set(ids)
    claimed = system.rows(
        "SELECT id FROM outbox WHERE id IN (:a, :b) AND published_at IS NULL FOR UPDATE SKIP LOCKED",
        a=world.a.outbox,
        b=world.b.outbox,
    )
    assert len(claimed) == 2
    expect(system, DENIED, "SELECT count(*) FROM projects")
    expect(system, DENIED, "SELECT output_key FROM build_tasks LIMIT 1")
    expect(system, DENIED, "SELECT count(*) FROM usage_entries")


@needs_system
def test_system_role_can_expire_a_lease(world: World, system: Db) -> None:
    # Lease, then expire it the way the reconciler will: state and lease_until change together (CHECK).
    system.run("UPDATE build_tasks SET state = 'leased', lease_until = now() WHERE id = :t", t=world.a.task)
    expect(system, REJECTED, "UPDATE build_tasks SET state = 'retry_wait' WHERE id = :t", t=world.a.task)
    system.run(
        "UPDATE build_tasks SET state = 'retry_wait', lease_until = NULL, next_attempt_at = now()"
        " WHERE id = :t",
        t=world.a.task,
    )
    assert system.one("SELECT state FROM build_tasks WHERE id = :t", t=world.a.task) == "retry_wait"


# ---------------------------------------------------------------- integrity
def test_integrity_rules(world: World, ddl: Db) -> None:
    expect(  # one active build per project
        ddl,
        REJECTED,
        "INSERT INTO builds(workspace_id, project_id, snapshot_id, config, config_digest, engine_version,"
        " prompt_version, cap_micro_usd, created_by, status)"
        " SELECT workspace_id, project_id, snapshot_id, '{}', :d, '0.1.0', '0.1.0', 10, created_by, 'running'"
        " FROM builds WHERE id = :b",
        d=secrets.token_hex(32),
        b=world.a.build,
    )
    expect(  # budget counters cannot exceed the cap
        ddl,
        REJECTED,
        "UPDATE builds SET reserved_micro_usd = 900, settled_micro_usd = 200 WHERE id = :b",
        b=world.a.build,
    )
    expect(  # artifact keys must live under their own workspace prefix
        ddl,
        REJECTED,
        "INSERT INTO artifacts(workspace_id, build_id, kind, object_key, sha256, byte_count, content_type)"
        " VALUES (:w, :b, 'manifest', :k, :sha, 1, 'application/json')",
        w=world.a.workspace,
        b=world.a.build,
        k=f"workspaces/{world.b.workspace}/x.json",
        sha="0" * 64,
    )
    a, b = world.a.workspace, world.b.workspace
    ddl.run(  # a well-formed key under the build's own workspace is accepted
        "INSERT INTO artifacts(workspace_id, build_id, kind, object_key, sha256, byte_count, content_type)"
        " VALUES (:w, :b, 'manifest', :k, :sha, 1, 'application/json')",
        w=a,
        b=world.a.build,
        k=f"workspaces/{a}/projects/p/builds/b/manifest.json",
        sha="0" * 64,
    )
    for bad_key in (
        f"workspaces/{a}/../{b}/x.json",
        f"workspaces/{a}/./x.json",
        f"workspaces/{a}//x.json",
        f"workspaces/{a}/x\\y.json",
        f"workspaces/{a}/x y.json",
        f"workspaces/{a}/..",
    ):
        expect(
            ddl,
            REJECTED,
            "INSERT INTO artifacts(workspace_id, build_id, kind, variant, object_key, sha256, byte_count,"
            " content_type) VALUES (:w, :b, 'export', :v, :k, :sha, 1, 'application/zip')",
            w=a,
            b=world.a.build,
            v=secrets.token_hex(4),
            k=bad_key,
            sha="0" * 64,
        )
    for bad in ("../etc/passwd", "/abs", "a//b", "a\\b", "x/../y"):
        expect(
            ddl,
            REJECTED,
            "INSERT INTO source_files(workspace_id, snapshot_id, path, byte_count, scan_status)"
            " VALUES (:w, :s, :p, 0, 'excluded')",
            w=world.a.workspace,
            s=world.a.snapshot,
            p=bad,
        )
