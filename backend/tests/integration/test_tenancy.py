"""Tenancy, identity and role boundaries of migration 0002 (T02; 05 "Tenant context resolution").

Ports build-package/contracts/core_schema_checks.sql to the real schema and extends it to the identity,
session and OAuth-transaction paths. Each check names the guarantee it protects.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
import sqlalchemy as sa

from tests.integration.conftest import RUNTIME_URL, Db, World, digest, engine, needs_db, needs_system

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
        "usage_accounts",
        "estimates",
        "provider_attempts",
    ):
        assert runtime.one(f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608 - fixed table names


def test_workspace_context_shows_only_that_tenant(world: World, runtime: Db) -> None:
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    assert {r[0] for r in runtime.rows("SELECT id FROM projects")} == {world.a.project}
    assert {r[0] for r in runtime.rows("SELECT id FROM builds")} == {world.a.build}
    assert {r[0] for r in runtime.rows("SELECT id FROM estimates")} == {world.a.estimate}
    assert {r[0] for r in runtime.rows("SELECT id FROM usage_accounts")} == {world.a.account}
    assert {r[0] for r in runtime.rows("SELECT id FROM provider_attempts")} == {world.a.attempt}
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
def binding_of(state: str) -> bytes:
    """The digest of the browser-binding cookie of the browser that started this flow."""
    return digest("browser-" + state)


def create_tx(db: Db, state: str, *, ttl: str = "10 minutes", purpose: str = "login", **kw: Any) -> None:
    db.run(
        "SELECT app_private.oauth_transaction_create(:st, :bind, 'google', :purpose, :nonce, NULL, :rp,"
        " :sess, NULL, CAST(:ttl AS interval))",
        st=digest(state),
        bind=binding_of(state),
        purpose=purpose,
        nonce=digest("n-" + state),
        rp=kw.get("return_path", "/"),
        sess=kw.get("session"),
        ttl=ttl,
    )


def test_oauth_state_is_single_use_and_bound_to_provider(world: World, runtime: Db) -> None:
    state = secrets.token_urlsafe(32)
    create_tx(runtime, state, return_path="/w/settings")
    consume = "SELECT return_path FROM app_private.oauth_transaction_consume(:st, :b, :p)"
    mine, theirs = binding_of(state), digest("another-browser")
    assert runtime.rows(consume, st=digest(state), b=mine, p="github") == []  # wrong provider
    # Another browser (login CSRF) is refused and does not burn the transaction for the real one.
    assert runtime.rows(consume, st=digest(state), b=theirs, p="google") == []
    assert runtime.rows(consume, st=digest(state), b=mine, p="google")[0][0] == "/w/settings"
    assert runtime.rows(consume, st=digest(state), b=mine, p="google") == []  # replay
    expect(runtime, DENIED, "SELECT count(*) FROM oauth_transactions")  # no direct access at all


def test_oauth_state_rules(world: World, runtime: Db) -> None:
    for ttl in ("11 minutes", "0 seconds", "-1 second"):
        expect(
            runtime,
            sa.exc.DataError,  # 22023 invalid_parameter_value
            "SELECT app_private.oauth_transaction_create(:st, :b, 'google', 'login', NULL, NULL, '/', NULL,"
            " NULL, CAST(:ttl AS interval))",
            st=digest(secrets.token_hex(8)),
            b=digest("b"),
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
        ddl.rows(
            "SELECT 1 FROM app_private.oauth_transaction_consume(:s, :b, 'google')",
            s=digest(state),
            b=binding_of(state),
        )
        == []
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
    expect(system, DENIED, "SELECT count(*) FROM provider_attempts")
    expect(system, DENIED, "SELECT count(*) FROM estimates")


def test_deletion_requests_are_scoped_and_claimed_without_content(world: World, runtime: Db) -> None:
    from tests.integration.auth_support import ddl as committed

    for side in (world.a, world.b):
        committed(
            "INSERT INTO deletion_requests(scope, workspace_id, project_id, not_before)"
            " VALUES ('project', :w, :p, now() - interval '1 minute')",
            w=side.workspace,
            p=side.project,
        )
    committed("INSERT INTO deletion_requests(scope, user_id) VALUES ('account', :u)", u=world.b.user)
    try:
        runtime.context(workspace=world.a.workspace, user=world.a.user)
        assert {r[0] for r in runtime.rows("SELECT project_id FROM deletion_requests")} == {world.a.project}
        expect(
            runtime,
            DENIED,
            "INSERT INTO deletion_requests(scope, workspace_id, project_id) VALUES ('project', :w, :p)",
            w=world.b.workspace,
            p=world.b.project,
        )
        expect(
            runtime,
            DENIED,
            "INSERT INTO deletion_requests(scope, user_id) VALUES ('account', :u)",
            u=world.b.user,
        )
        runtime.context(user=world.b.user)
        assert runtime.one("SELECT count(*) FROM deletion_requests") == 1  # own account request only
        assert runtime.one("SELECT count(*) FROM deletion_requests WHERE user_id IS NULL") == 0
        # The purger's claim crosses tenants but returns identifiers only (its own, rolled back connection).
        with engine(RUNTIME_URL).connect() as conn, conn.begin() as tx:
            claimed = []
            while rows := list(conn.execute(sa.text("SELECT * FROM app_private.claim_deletion(60)"))):
                claimed.extend(rows)
            tx.rollback()
        assert {world.a.project, world.b.project} <= {r.project_id for r in claimed}
        assert set(claimed[0]._fields) == {
            "id",
            "scope",
            "workspace_id",
            "project_id",
            "user_id",
            "attempts",
            "requested_at",
        }
    finally:
        committed(
            "DELETE FROM deletion_requests WHERE workspace_id IN (:a, :b) OR user_id = :u",
            a=world.a.workspace,
            b=world.b.workspace,
            u=world.b.user,
        )


def test_the_runtime_cannot_grant_itself_operations_rights(world: World, runtime: Db) -> None:
    """0018: whatever default privileges a database was bootstrapped with, the runtime role cannot make
    anyone an operator, flip platform switches, write entitlements or delete tombstones."""
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    expect(runtime, DENIED, "INSERT INTO operators(user_id) VALUES (:u)", u=world.a.user)
    expect(runtime, DENIED, "SELECT count(*) FROM operators")
    expect(runtime, DENIED, "SELECT count(*) FROM operator_events")
    expect(runtime, DENIED, "UPDATE platform_settings SET builds_paused = false")
    assert runtime.one("SELECT count(*) FROM platform_settings") == 1  # reading the switch is allowed
    expect(
        runtime,
        DENIED,
        "INSERT INTO entitlement_grants(workspace_id, allowance_micro_usd, expires_at, reason, granted_by)"
        " VALUES (:w, 1000000, now() + interval '1 day', 'self-service', :u)",
        w=world.a.workspace,
        u=world.a.user,
    )
    expect(runtime, DENIED, "DELETE FROM deletion_requests WHERE workspace_id = :w", w=world.a.workspace)
    expect(runtime, DENIED, "DELETE FROM support_grants WHERE workspace_id = :w", w=world.a.workspace)
    assert runtime.one("SELECT app_private.is_operator()") is False


def test_billing_rows_are_tenant_scoped_and_trials_are_owner_only(world: World, runtime: Db) -> None:
    """0022: customers and subscriptions follow the tenant policy; a webhook's customer resolves to its
    workspace without exposing rows; only an owner starts the one trial, and never by writing grants."""
    customer_a, customer_b = f"cus_a{secrets.token_hex(6)}", f"cus_b{secrets.token_hex(6)}"
    for tenant, customer in ((world.a, customer_a), (world.b, customer_b)):
        runtime.context(workspace=tenant.workspace, user=tenant.user)
        runtime.run(
            "INSERT INTO billing_customers(workspace_id, provider_customer_id) VALUES (:w, :c)",
            w=tenant.workspace,
            c=customer,
        )
        runtime.run(
            "INSERT INTO subscriptions(workspace_id, provider_subscription_id, provider_customer_id, plan_key,"
            " status) VALUES (:w, :s, :c, 'pro', 'active')",
            w=tenant.workspace,
            s=f"sub_{secrets.token_hex(6)}",
            c=customer,
        )
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    assert (
        runtime.one("SELECT count(*) FROM billing_customers WHERE workspace_id = :w", w=world.b.workspace)
        == 0
    )
    assert runtime.one("SELECT count(*) FROM subscriptions WHERE workspace_id = :w", w=world.b.workspace) == 0
    expect(
        runtime,
        DENIED,
        "INSERT INTO subscriptions(workspace_id, provider_subscription_id, provider_customer_id, plan_key, status)"
        " VALUES (:w, :s, :c, 'team', 'active')",
        w=world.b.workspace,
        s=f"sub_{secrets.token_hex(6)}",
        c=customer_b,
    )
    # Only the dispute flag is writable on a customer; the mapping to a workspace is fixed.
    expect(
        runtime,
        DENIED,
        "UPDATE billing_customers SET provider_customer_id = 'cus_x' WHERE workspace_id = :w",
        w=world.a.workspace,
    )
    expect(runtime, DENIED, "DELETE FROM subscriptions WHERE workspace_id = :w", w=world.a.workspace)
    runtime.context()
    assert runtime.one("SELECT app_private.billing_workspace(:c)", c=customer_b) == world.b.workspace
    assert runtime.one("SELECT app_private.billing_workspace('cus_nobody')") is None
    assert runtime.one("SELECT count(*) FROM billing_customers") == 0  # the resolver exposes no rows

    # world.b's owner cannot start a trial in world.a, and world.a has a subscription: no trial there either.
    runtime.context(workspace=world.a.workspace, user=world.b.user)
    expect(runtime, DENIED, "SELECT app_private.start_trial(:w, 1, 1)", w=world.a.workspace)
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    assert runtime.one("SELECT app_private.start_trial(:w, 1, 1)", w=world.a.workspace) is None


def test_push_requests_are_tenant_scoped_and_targets_come_from_the_resolver(
    world: World, runtime: Db
) -> None:
    """0024: push requests follow the tenant policy; the push resolvers return identifiers only."""
    runtime.context(workspace=world.b.workspace, user=world.b.user)
    runtime.run(
        "INSERT INTO push_requests(workspace_id, project_id, commit_sha, ref, due_at)"
        " VALUES (:w, :p, :sha, 'main', now())",
        w=world.b.workspace,
        p=world.b.project,
        sha="a" * 40,
    )
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    assert runtime.one("SELECT count(*) FROM push_requests WHERE workspace_id = :w", w=world.b.workspace) == 0
    expect(
        runtime,
        DENIED,
        "INSERT INTO push_requests(workspace_id, project_id, commit_sha, ref, due_at)"
        " VALUES (:w, :p, :sha, 'main', now())",
        w=world.b.workspace,
        p=world.b.project,
        sha="b" * 40,
    )
    expect(runtime, DENIED, "DELETE FROM push_requests WHERE workspace_id = :w", w=world.a.workspace)
    # Only the policy columns of a project are writable for push settings; the policy needs its admin.
    expect(
        runtime,
        REJECTED,
        "UPDATE projects SET push_policy = 'build', push_policy_by = NULL WHERE id = :p",
        p=world.a.project,
    )
    runtime.context()
    due = runtime.rows("SELECT * FROM app_private.due_push_requests(500)")
    assert all(set(r._mapping) == {"workspace_id", "request_id"} for r in due)
    assert runtime.one("SELECT count(*) FROM push_requests") == 0  # the resolver exposes no rows


def test_provider_keys_and_batches_are_tenant_scoped(world: World, runtime: Db) -> None:
    """0026: sealed keys and provider batches follow the tenant policy; only the seal and revocation of a
    key can change, so a key cannot be repointed to another hint, creator or workspace."""
    runtime.context(workspace=world.b.workspace, user=world.b.user)
    runtime.run(
        "INSERT INTO provider_keys(workspace_id, provider, sealed_key, hint, created_by)"
        " VALUES (:w, 'anthropic', '\\x00'::bytea, 'sk-ant-…zzzz', :u)",
        w=world.b.workspace,
        u=world.b.user,
    )
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    assert runtime.one("SELECT count(*) FROM provider_keys WHERE workspace_id = :w", w=world.b.workspace) == 0
    expect(
        runtime,
        DENIED,
        "INSERT INTO provider_keys(workspace_id, provider, sealed_key, hint, created_by)"
        " VALUES (:w, 'anthropic', '\\x00'::bytea, 'x', :u)",
        w=world.b.workspace,
        u=world.a.user,
    )
    expect(
        runtime, DENIED, "UPDATE provider_keys SET hint = 'x' WHERE workspace_id = :w", w=world.a.workspace
    )
    expect(runtime, DENIED, "DELETE FROM provider_keys WHERE workspace_id = :w", w=world.a.workspace)
    expect(runtime, DENIED, "DELETE FROM provider_batches WHERE workspace_id = :w", w=world.a.workspace)
    assert (
        runtime.one("SELECT count(*) FROM provider_batches WHERE workspace_id = :w", w=world.b.workspace) == 0
    )


def test_project_grants_are_tenant_scoped(world: World, runtime: Db) -> None:
    """0027: a project grant belongs to one workspace and can only name that workspace's members."""
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    expect(
        runtime,
        DENIED,
        "INSERT INTO project_members(workspace_id, project_id, user_id, role, granted_by)"
        " VALUES (:w, :p, :u, 'editor', :u)",
        w=world.b.workspace,
        p=world.b.project,
        u=world.a.user,
    )
    expect(  # a member of workspace B cannot be granted a project of workspace A
        runtime,
        REJECTED,
        "INSERT INTO project_members(workspace_id, project_id, user_id, role, granted_by)"
        " VALUES (:w, :p, :u, 'editor', :by)",
        w=world.a.workspace,
        p=world.a.project,
        u=world.b.user,
        by=world.a.user,
    )
    expect(runtime, DENIED, "UPDATE project_members SET user_id = :u", u=world.b.user)
    assert (
        runtime.one("SELECT count(*) FROM project_members WHERE workspace_id = :w", w=world.b.workspace) == 0
    )


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


def test_every_tenant_table_forces_rls_with_a_runtime_tenant_policy(
    world: World, ddl: Db, runtime: Db
) -> None:
    """Schema-wide guard: a new table with workspace_id cannot ship without FORCE RLS and a policy that
    scopes the runtime role to app.workspace_id (CLAUDE.md "Schema and models")."""
    tables = [
        r[0]
        for r in ddl.rows(
            "SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables t"
            " ON t.table_schema = c.table_schema AND t.table_name = c.table_name"
            " WHERE c.table_schema = 'public' AND c.column_name = 'workspace_id' AND t.table_type = 'BASE TABLE'"
            " ORDER BY 1"
        )
    ]
    assert {
        "generation_cache",
        "file_explanations",
        "guide_pages",
        "claim_checks",
        "search_documents",
    } <= set(tables)
    for table in tables:
        forced = ddl.one(
            "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = CAST(:t AS regclass)",
            t=table,
        )
        assert forced, table
        if not ddl.one("SELECT has_table_privilege('app_runtime', CAST(:t AS regclass), 'SELECT')", t=table):
            continue  # unreachable for the runtime role (oauth_transactions: lookup functions only)
        policies = ddl.rows(
            "SELECT COALESCE(qual, '') || ' ' || COALESCE(with_check, '') FROM pg_policies"
            " WHERE schemaname = 'public' AND tablename = :t AND 'app_runtime' = ANY(roles)",
            t=table,
        )
        # Tenant rows are scoped by the workspace context; idempotency keys by their user (05).
        assert any("app.workspace_id" in q or "app.user_id" in q for (q,) in policies), table
        assert runtime.one(f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608 - names from the catalog


def test_owner_notices_are_tenant_scoped_and_found_by_the_resolver(world: World, runtime: Db) -> None:
    """0034: owner notices follow the tenant policy; only their delivery columns change, and the due-notice
    resolver returns identifiers only."""
    runtime.context(workspace=world.b.workspace, user=world.b.user)
    runtime.run(
        "INSERT INTO owner_notices(workspace_id, kind, dedupe_key) VALUES (:w, 'dispute_opened', 'x')",
        w=world.b.workspace,
    )
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    assert runtime.one("SELECT count(*) FROM owner_notices WHERE workspace_id = :w", w=world.b.workspace) == 0
    expect(
        runtime,
        DENIED,
        "INSERT INTO owner_notices(workspace_id, kind, dedupe_key) VALUES (:w, 'dispute_opened', 'y')",
        w=world.b.workspace,
    )
    runtime.run(
        "INSERT INTO owner_notices(workspace_id, kind, dedupe_key) VALUES (:w, 'dispute_lost', 'z')",
        w=world.a.workspace,
    )
    expect(
        runtime,
        DENIED,
        "UPDATE owner_notices SET kind = 'dispute_opened' WHERE workspace_id = :w",
        w=world.a.workspace,
    )
    expect(runtime, DENIED, "UPDATE owner_notices SET workspace_id = :b", b=world.b.workspace)
    runtime.run(
        "UPDATE owner_notices SET state = 'sent', sent_at = now() WHERE workspace_id = :w",
        w=world.a.workspace,
    )
    runtime.context()
    due = runtime.rows("SELECT * FROM app_private.due_owner_notices(500)")
    assert all(set(r._mapping) == {"workspace_id", "notice_id"} for r in due)
    assert world.b.workspace in {r.workspace_id for r in due}
    assert runtime.one("SELECT count(*) FROM owner_notices") == 0  # the resolver exposes no rows


def test_access_tokens_are_tenant_scoped_and_resolved_by_digest_only(world: World, runtime: Db) -> None:
    """0035: tokens follow the tenant policy; only use and revocation change; the resolver answers a
    digest with identifiers, and only while the token's user is still a member."""
    digest = b"\x01" * 32
    runtime.context(workspace=world.b.workspace, user=world.b.user)
    runtime.run(
        "INSERT INTO access_tokens(workspace_id, user_id, name, token_hash, prefix, expires_at)"
        " VALUES (:w, :u, 't', :h, 'chm_abcd1234', now() + interval '1 day')",
        w=world.b.workspace,
        u=world.b.user,
        h=digest,
    )
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    assert runtime.one("SELECT count(*) FROM access_tokens WHERE workspace_id = :w", w=world.b.workspace) == 0
    expect(
        runtime,
        DENIED,
        "INSERT INTO access_tokens(workspace_id, user_id, name, token_hash, prefix, expires_at)"
        " VALUES (:w, :u, 't', :h, 'chm_abcd1234', now() + interval '1 day')",
        w=world.b.workspace,
        u=world.a.user,
        h=b"\x02" * 32,
    )
    expect(runtime, DENIED, "UPDATE access_tokens SET user_id = :u", u=world.a.user)
    expect(runtime, DENIED, "UPDATE access_tokens SET expires_at = now() + interval '9 years'")
    runtime.context()
    found = runtime.rows("SELECT * FROM app_private.access_token(:h)", h=digest)
    assert [(r.workspace_id, r.user_id) for r in found] == [(world.b.workspace, world.b.user)]
    assert set(found[0]._mapping) == {"token_id", "workspace_id", "user_id", "scope", "project_id"}
    assert runtime.rows("SELECT * FROM app_private.access_token(:h)", h=b"\x03" * 32) == []
    assert runtime.one("SELECT count(*) FROM access_tokens") == 0  # the resolver exposes no rows


PUBLISH = (
    "INSERT INTO publications(workspace_id, project_id, build_id, visibility, public_slug, published_by)"
    " VALUES (:w, :p, :b, 'public', :s, :u)"
)


def _publish(db: Db, tenant: Any, address: str) -> None:
    db.context(workspace=tenant.workspace, user=tenant.user)
    db.run(PUBLISH, w=tenant.workspace, p=tenant.project, b=tenant.build, s=address, u=tenant.user)


def _refused(db: Db, tenant: Any, address: str) -> None:
    db.context(workspace=tenant.workspace, user=tenant.user)
    savepoint = db.conn.begin_nested()
    with pytest.raises(REJECTED, match="released recently and is reserved"):
        db.run(PUBLISH, w=tenant.workspace, p=tenant.project, b=tenant.build, s=address, u=tenant.user)
    savepoint.rollback()


def test_a_released_site_address_stays_reserved_for_its_workspace(world: World, runtime: Db) -> None:
    """0036: an address let go by renaming or unpublishing is reserved for its workspace; another
    workspace cannot take it, the same one can, and the runtime never sees the reservations."""
    slug, other = f"site-{secrets.token_hex(3)}", f"next-{secrets.token_hex(3)}"
    rename = "UPDATE publications SET public_slug = :s WHERE workspace_id = :w"
    _publish(runtime, world.a, slug)
    runtime.run(rename, s=other, w=world.a.workspace)
    _refused(runtime, world.b, slug)
    runtime.context(workspace=world.a.workspace, user=world.a.user)  # its own workspace takes it back
    runtime.run(rename, s=slug, w=world.a.workspace)
    _refused(runtime, world.b, other)  # the address it just left is reserved now
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    runtime.run(
        "UPDATE publications SET visibility = 'private', public_slug = NULL WHERE workspace_id = :w",
        w=world.a.workspace,
    )
    _refused(runtime, world.b, slug)  # a private site without an address lets go of it too
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    runtime.run(rename, s=slug, w=world.a.workspace)
    runtime.run("DELETE FROM publications WHERE workspace_id = :w", w=world.a.workspace)  # unpublish
    _refused(runtime, world.b, slug)
    expect(runtime, DENIED, "SELECT count(*) FROM slug_reservations")
    expect(runtime, DENIED, "DELETE FROM slug_reservations")
    _publish(runtime, world.a, slug)  # still its own


def test_a_slug_reservation_ends_after_its_period(world: World, ddl: Db) -> None:
    """0036: once the 90 days have passed, any workspace may take the address; the sweep removes ended
    reservations."""
    slug, other = f"site-{secrets.token_hex(3)}", f"next-{secrets.token_hex(3)}"
    _publish(ddl, world.a, slug)
    ddl.run("DELETE FROM publications WHERE workspace_id = :w", w=world.a.workspace)
    row = ddl.rows(
        "SELECT workspace_id, reserved_until - released_at AS span FROM slug_reservations WHERE slug = :s",
        s=slug,
    )
    assert [(r.workspace_id, r.span.days) for r in row] == [(world.a.workspace, 90)]
    _refused(ddl, world.b, slug)
    ended = (
        "UPDATE slug_reservations SET released_at = now() - interval '91 days',"
        " reserved_until = now() - interval '1 day' WHERE slug = :s"
    )
    ddl.run(ended, s=slug)
    _publish(ddl, world.b, slug)
    assert ddl.one("SELECT count(*) FROM slug_reservations WHERE slug = :s", s=slug) == 0
    ddl.run("UPDATE publications SET public_slug = :s WHERE workspace_id = :w", s=other, w=world.b.workspace)
    ddl.run(ended, s=slug)
    assert ddl.one("SELECT app_private.sweep_slug_reservations()") >= 1
    assert ddl.one("SELECT count(*) FROM slug_reservations WHERE slug = :s", s=slug) == 0


def test_a_project_keeps_its_last_three_released_addresses(world: World, ddl: Db) -> None:
    """0036 security review: renaming a site through many addresses cannot reserve them all; each project
    keeps the three it released last, so the address it just let go is never the one that ends."""
    run = secrets.token_hex(3)
    _publish(ddl, world.a, f"r{run}-00")
    for n in range(1, 13):  # each rename releases the previous address
        ddl.run(
            "UPDATE publications SET public_slug = :s WHERE workspace_id = :w",
            s=f"r{run}-{n:02}",
            w=world.a.workspace,
        )
    held = [
        r[0]
        for r in ddl.rows(
            "SELECT slug FROM slug_reservations WHERE project_id = :p ORDER BY slug", p=world.a.project
        )
    ]
    assert held == [f"r{run}-{n:02}" for n in (9, 10, 11)]
    ddl.run("DELETE FROM publications WHERE workspace_id = :w", w=world.a.workspace)  # unpublish
    assert ddl.one("SELECT count(*) FROM slug_reservations WHERE slug = :s", s=f"r{run}-12") == 1
    _publish(ddl, world.b, f"r{run}-00")  # long released by now
    _refused(ddl, world.b, f"r{run}-12")


def test_mcp_sign_in_rows_are_scoped_and_reached_through_functions(world: World, runtime: Db) -> None:
    """0037: clients and pending authorizations are global and unreachable for the runtime; grants follow
    the tenant policy and change only in their refresh, use and revocation columns; a code is redeemed
    once, a second use ends the grant made from it."""
    for table in ("oauth_clients", "oauth_authorizations"):
        expect(runtime, DENIED, f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
    client_id = "chc_" + secrets.token_urlsafe(24)
    runtime.one("SELECT app_private.oauth_register_client(:c, 'x', ARRAY['https://x.test/cb'])", c=client_id)
    client = runtime.one("SELECT id FROM app_private.oauth_client(:c)", c=client_id)
    request = runtime.one(
        "SELECT app_private.oauth_begin(:c, 'https://x.test/cb', :ch, 'r', 'some-site', NULL)",
        c=client,
        ch="A" * 43,
    )
    assert runtime.rows("SELECT * FROM app_private.oauth_pending(:i)", i=request)[0].slug == "some-site"
    runtime.context()  # nobody signed in cannot decide
    expect(
        runtime,
        DENIED,
        "SELECT * FROM app_private.oauth_decide(:i, :w, :p, true, :h)",
        i=request,
        w=world.a.workspace,
        p=world.a.project,
        h=b"\x05" * 32,
    )
    runtime.context(user=world.b.user)  # someone without access to the project cannot approve
    expect(
        runtime,
        DENIED,
        "SELECT * FROM app_private.oauth_decide(:i, :w, :p, true, :h)",
        i=request,
        w=world.a.workspace,
        p=world.a.project,
        h=b"\x05" * 32,
    )
    runtime.context(user=world.a.user)
    assert runtime.rows(
        "SELECT * FROM app_private.oauth_decide(:i, :w, :p, true, :h)",
        i=request,
        w=world.a.workspace,
        p=world.a.project,
        h=b"\x05" * 32,
    )
    redeemed = runtime.rows(
        "SELECT * FROM app_private.oauth_redeem(:h, :c, 'https://x.test/cb')", h=b"\x05" * 32, c=client_id
    )
    assert [(r.workspace_id, r.user_id, r.project_id) for r in redeemed] == [
        (world.a.workspace, world.a.user, world.a.project)
    ]
    runtime.context(workspace=world.a.workspace, user=world.a.user)
    runtime.run(
        "INSERT INTO oauth_grants(workspace_id, user_id, project_id, client, client_name, authorization_id,"
        " refresh_hash, refresh_expires_at) VALUES (:w, :u, :p, :c, 'x', :a, :h, now() + interval '1 day')",
        w=world.a.workspace,
        u=world.a.user,
        p=world.a.project,
        c=client,
        a=redeemed[0].authorization_id,
        h=b"\x06" * 32,
    )
    expect(runtime, DENIED, "UPDATE oauth_grants SET project_id = :p", p=world.b.project)
    expect(runtime, DENIED, "UPDATE oauth_grants SET user_id = :u", u=world.b.user)
    runtime.context(workspace=world.b.workspace, user=world.b.user)
    assert runtime.one("SELECT count(*) FROM oauth_grants") == 0
    expect(
        runtime,
        DENIED,
        "INSERT INTO oauth_grants(workspace_id, user_id, project_id, client, client_name, authorization_id,"
        " refresh_hash, refresh_expires_at) VALUES (:w, :u, :p, :c, 'x', :a, :h, now() + interval '1 day')",
        w=world.a.workspace,
        u=world.b.user,
        p=world.a.project,
        c=client,
        a=request,
        h=b"\x07" * 32,
    )
    runtime.context()
    assert (
        runtime.rows(
            "SELECT * FROM app_private.oauth_redeem(:h, :c, 'https://x.test/cb')", h=b"\x05" * 32, c=client_id
        )
        == []
    )
    runtime.context(workspace=world.a.workspace)
    assert runtime.one(
        "SELECT revoked_at IS NOT NULL FROM oauth_grants WHERE refresh_hash = :h", h=b"\x06" * 32
    )
