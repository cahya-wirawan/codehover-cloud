"""Members' monthly spending limits in a workspace (0044, 09 "Spending limits", 11).

Owners and admins may give any member a monthly limit; admins only for editors and viewers, like role
changes. A limit counts what the member's own allowance-funded builds in this workspace hold or have
settled in the current usage period (builds on the workspace's own key are not counted: the provider bills
the workspace directly). Build admission checks it under the owner's lock (ledger.current_account), so two
starts cannot both use the same room. A limit ends with the membership.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.modules.workspaces.access import Member
from app.modules.workspaces.service import audit, can_manage

MAX_LIMIT_MICRO_USD = 1_000_000_000


@dataclass(frozen=True)
class MemberRoom:
    limit_micro_usd: int
    used_micro_usd: int

    @property
    def room_micro_usd(self) -> int:
        return max(0, self.limit_micro_usd - self.used_micro_usd)


async def used(db: AsyncSession, workspace_id: UUID, account_id: UUID, user_id: UUID) -> int:
    """What the member's builds charged to this period's account hold or have settled."""
    value = (
        await db.execute(
            text(
                "SELECT COALESCE(sum(reserved_micro_usd + settled_micro_usd), 0) FROM builds"
                " WHERE workspace_id = :w AND usage_account_id = :a AND created_by = :u"
            ),
            {"w": workspace_id, "a": account_id, "u": user_id},
        )
    ).scalar_one()
    return int(value)


async def room(db: AsyncSession, workspace_id: UUID, account_id: UUID, user_id: UUID) -> MemberRoom | None:
    """The member's limit and use this period; None when the member has no limit."""
    limit = (
        await db.execute(
            text(
                "SELECT monthly_limit_micro_usd FROM member_limits WHERE workspace_id = :w AND user_id = :u"
            ),
            {"w": workspace_id, "u": user_id},
        )
    ).scalar_one_or_none()
    if limit is None:
        return None
    return MemberRoom(int(limit), await used(db, workspace_id, account_id, user_id))


async def by_member(db: AsyncSession, workspace_id: UUID, account_id: UUID) -> list[dict[str, Any]]:
    """Every member with this period's builds and spending in the workspace, and their limit."""
    rows = await db.execute(
        text(
            "SELECT m.user_id, u.display_name, n.handle, m.role, l.monthly_limit_micro_usd AS limit_micro_usd,"
            " count(b.id) AS builds, COALESCE(sum(b.settled_micro_usd), 0) AS settled_micro_usd,"
            " COALESCE(sum(b.reserved_micro_usd), 0) AS reserved_micro_usd"
            " FROM memberships m JOIN users u ON u.id = m.user_id"
            " LEFT JOIN namespaces n ON n.user_id = m.user_id"
            " LEFT JOIN member_limits l ON l.workspace_id = m.workspace_id AND l.user_id = m.user_id"
            " LEFT JOIN builds b ON b.workspace_id = m.workspace_id AND b.usage_account_id = :a"
            "   AND b.created_by = m.user_id"
            " WHERE m.workspace_id = :w"
            " GROUP BY m.user_id, u.display_name, n.handle, m.role, l.monthly_limit_micro_usd, m.created_at"
            " ORDER BY m.created_at, m.user_id"
        ),
        {"w": workspace_id, "a": account_id},
    )
    return [dict(r._mapping) for r in rows]


async def set_limit(db: AsyncSession, member: Member, user_id: UUID, limit_micro_usd: int | None) -> None:
    """Set or remove a member's limit (the caller checked admin; this checks who may limit whom)."""
    # Lock the membership: limit changes, role changes and removals of one member serialize.
    target = (
        await db.execute(
            text("SELECT role FROM memberships WHERE workspace_id = :w AND user_id = :u FOR UPDATE"),
            {"w": member.workspace_id, "u": user_id},
        )
    ).scalar_one_or_none()
    if target is None:
        raise ApiError(404, "NOT_FOUND", "Member not found.")
    if not can_manage(member, str(target)):
        raise ApiError(403, "FORBIDDEN", "Your role does not allow limiting this member.")
    previous = (
        await db.execute(
            text(
                "SELECT monthly_limit_micro_usd FROM member_limits WHERE workspace_id = :w AND user_id = :u"
            ),
            {"w": member.workspace_id, "u": user_id},
        )
    ).scalar_one_or_none()
    if previous == limit_micro_usd:
        return
    if limit_micro_usd is None:
        await db.execute(
            text("DELETE FROM member_limits WHERE workspace_id = :w AND user_id = :u"),
            {"w": member.workspace_id, "u": user_id},
        )
    else:
        await db.execute(
            text(
                "INSERT INTO member_limits(workspace_id, user_id, monthly_limit_micro_usd, set_by)"
                " VALUES (:w, :u, :n, :by) ON CONFLICT (workspace_id, user_id) DO UPDATE"
                " SET monthly_limit_micro_usd = EXCLUDED.monthly_limit_micro_usd, set_by = EXCLUDED.set_by,"
                " updated_at = now()"
            ),
            {"w": member.workspace_id, "u": user_id, "n": limit_micro_usd, "by": member.user_id},
        )
    await audit(
        db,
        member,
        "member.spending_limit_changed",
        "membership",
        None,
        {"user_id": str(user_id), "from": previous, "to": limit_micro_usd},
    )
