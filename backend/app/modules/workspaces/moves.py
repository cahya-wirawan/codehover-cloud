"""Moving a workspace to another owner (0042; 02 S03b, 06 POST /workspaces/{wid}/move).

A workspace owner moves it to their own namespace or to an organization they own or administer. Its
projects keep their slugs under the new owner, and each old site address keeps leading to its project for
90 days. The move is refused when a project's slug is taken under the new owner (the clashes are listed;
nothing is renamed), when the workspace holds its owner's billing (hand that to another workspace first),
and, with `BILLING_ENFORCED`, when the new owner's plan has no room for its projects and people. The
database function `app_private.move_workspace` re-checks the caller and does the move under the locks.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.settings import Settings
from app.core.tenancy import set_context
from app.modules.billing import service as billing
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit


async def _target_namespace(db: AsyncSession, member: Member, organization_id: UUID | None) -> UUID | None:
    query = (
        "SELECT id FROM namespaces WHERE user_id = :u"
        if organization_id is None
        else "SELECT id FROM namespaces WHERE organization_id = :o"
    )
    value = (await db.execute(text(query), {"u": member.user_id, "o": organization_id})).scalar_one_or_none()
    return value if value is None or isinstance(value, UUID) else UUID(str(value))


async def _handle(db: AsyncSession, namespace_id: UUID | None) -> str | None:
    value = (
        await db.execute(text("SELECT handle FROM namespaces WHERE id = :n"), {"n": namespace_id})
    ).scalar_one_or_none()
    return None if value is None else str(value)


async def _room_after_move(db: AsyncSession, workspace_id: UUID) -> None:
    """The new owner's plan limits, counted with the moved workspace (11; nothing is added by a move, so
    only a count above the limit refuses it)."""
    state = await billing.state(db, workspace_id)
    if state.plan is None:
        raise ApiError(
            402, "PLAN_REQUIRED", "The new owner needs a plan or the trial before workspaces move to it."
        )
    totals = (await db.execute(text("SELECT * FROM app_private.owner_totals()"))).one()
    for kind, used, limit in (
        ("projects", totals.projects, state.plan.projects),
        ("seats", totals.people, state.plan.seats),
    ):
        if used > limit:
            raise ApiError(
                409,
                "PLAN_LIMIT",
                f"The new owner's {state.plan.name} plan allows {limit} {kind}; with this workspace it would"
                f" have {used}.",
                details={"limit": limit, "kind": kind},
            )


async def move_workspace(
    db: AsyncSession, settings: Settings, member: Member, organization_id: UUID | None
) -> None:
    if member.support or member.role != "owner":
        raise ApiError(403, "FORBIDDEN", "Only an owner of the workspace moves it.")
    workspace_id = member.workspace_id
    source = (
        await db.execute(text("SELECT namespace_id FROM workspaces WHERE id = :w"), {"w": workspace_id})
    ).scalar_one_or_none()
    target = await _target_namespace(db, member, organization_id)
    from_handle, to_handle = await _handle(db, source), await _handle(db, target)
    await set_context(db, user_id=member.user_id)
    try:
        await db.execute(
            text("SELECT app_private.move_workspace(:w, :o)"), {"w": workspace_id, "o": organization_id}
        )
    except exc.IntegrityError as error:
        await db.rollback()
        await set_context(db, user_id=member.user_id)
        clashes = [
            {"project_id": str(r.project_id), "name": r.name, "slug": r.slug}
            for r in await db.execute(
                text("SELECT * FROM app_private.workspace_move_clashes(:w, :t)"),
                {"w": workspace_id, "t": target},
            )
        ]
        await db.rollback()
        raise ApiError(
            409,
            "SLUG_TAKEN",
            f"{to_handle or 'The new owner'} already has projects at these addresses. Change their addresses"
            " in this workspace, then move it.",
            details={"projects": clashes},
        ) from error
    except exc.DBAPIError as error:
        await db.rollback()
        state = getattr(error.orig, "sqlstate", "")
        if state == "42501":
            raise ApiError(
                403,
                "FORBIDDEN",
                "You can move it to yourself, or to an organization you own or administer.",
            ) from error
        if state == "22023":
            raise ApiError(422, "SAME_OWNER", "The workspace already belongs to that owner.") from error
        if state == "55000":
            raise ApiError(
                409,
                "BILLING_HELD",
                "This workspace holds its owner's billing. Hand it to another of the owner's workspaces on its"
                " Plan and usage page first.",
            ) from error
        raise
    await set_context(db, workspace_id=workspace_id, user_id=member.user_id)
    if settings.billing_enforced:
        try:
            await _room_after_move(db, workspace_id)
        except ApiError:
            await db.rollback()
            raise
    await audit(
        db,
        member,
        "workspace.moved",
        "workspace",
        workspace_id,
        {"from_owner": from_handle, "to_owner": to_handle},
    )
    await db.commit()
