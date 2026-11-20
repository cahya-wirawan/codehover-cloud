"""Single-envelope budget ledger (11 "At build creation ...", 05 "Budget counters").

- reserve_build: at build creation, lock the period's usage account, check
  settled + reserved + cap <= allowance, and hold the whole cap once.
- settle_attempt (provider gateway, T11): move an attempt's actual cost from reserved to settled on the
  build and its account, release the attempt's earmark.
- release_build: at termination, return the unspent envelope except holds for attempts whose provider
  outcome is unknown.

Every transition writes one append-only `usage_entries` row with a unique idempotency key in the same
transaction as the counter change; a repeated transition finds its key and changes nothing.
Amounts are integer USD micro-units.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.billing.plans import HOLDS_PLAN, PLANS


@dataclass(frozen=True)
class Account:
    id: UUID
    period_start: datetime
    period_end: datetime
    allowance_micro_usd: int
    reserved_micro_usd: int
    settled_micro_usd: int

    @property
    def available_micro_usd(self) -> int:
        return max(0, self.allowance_micro_usd - self.reserved_micro_usd - self.settled_micro_usd)


_ACCOUNT_SQL = (
    "SELECT id, period_start, period_end, allowance_micro_usd, reserved_micro_usd, settled_micro_usd"
    " FROM usage_accounts WHERE workspace_id = :w AND period_start = :s"
)
_ACCOUNT = text(_ACCOUNT_SQL)
_ACCOUNT_FOR_UPDATE = text(_ACCOUNT_SQL + " FOR UPDATE")


def period_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    return start, end


async def current_account(
    db: AsyncSession, workspace_id: UUID, default_allowance: int, *, lock: bool
) -> Account:
    """The account of the current calendar month (UTC), created with the operator default allowance."""
    start, end = period_bounds(datetime.now(UTC))
    await db.execute(
        text(
            "INSERT INTO usage_accounts(workspace_id, period_start, period_end, allowance_micro_usd)"
            " VALUES (:w, :s, :e, :a) ON CONFLICT (workspace_id, period_start) DO NOTHING"
        ),
        {"w": workspace_id, "s": start, "e": end, "a": default_allowance},
    )
    query = _ACCOUNT_FOR_UPDATE if lock else _ACCOUNT
    row = (await db.execute(query, {"w": workspace_id, "s": start})).one()
    # Manual entitlements (11, 0018) add to every period they overlap; a one-time grant (the trial, 0022)
    # counts only in the period it starts; revoked grants stop counting.
    granted = (
        await db.execute(
            text(
                "SELECT COALESCE(sum(allowance_micro_usd), 0) FROM entitlement_grants WHERE workspace_id = :w"
                " AND revoked_at IS NULL AND starts_at < :e AND expires_at > :s AND (NOT once OR starts_at >= :s)"
            ),
            {"w": workspace_id, "s": start, "e": end},
        )
    ).scalar_one()
    # A plan held through the provider replaces the operator default with the plan's monthly allowance
    # (11). Past due keeps the plan; generation is blocked separately (billing.service).
    plan_key = (
        await db.execute(
            text(
                "SELECT plan_key FROM subscriptions WHERE workspace_id = :w AND status = ANY(:held)"
                " ORDER BY updated_at DESC LIMIT 1"
            ),
            {"w": workspace_id, "held": list(HOLDS_PLAN)},
        )
    ).scalar_one_or_none()
    base = PLANS[plan_key].allowance_micro_usd if plan_key in PLANS else default_allowance
    allowance = base + int(granted)
    if lock and allowance != row.allowance_micro_usd:  # persisted under the account lock
        await db.execute(
            text("UPDATE usage_accounts SET allowance_micro_usd = :a, version = version + 1 WHERE id = :id"),
            {"a": allowance, "id": row.id},
        )
    return Account(
        row.id, row.period_start, row.period_end, allowance, row.reserved_micro_usd, row.settled_micro_usd
    )


async def _entry(
    db: AsyncSession,
    workspace_id: UUID,
    account_id: UUID,
    build_id: UUID | None,
    kind: str,
    amount: int,
    key: str,
) -> bool:
    """Append a ledger row; False when this transition was already recorded (idempotent replay)."""
    inserted = (
        await db.execute(
            text(
                "INSERT INTO usage_entries(workspace_id, account_id, build_id, kind, amount_micro_usd, idempotency_key)"
                " VALUES (:w, :a, :b, :k, :n, :key) ON CONFLICT (workspace_id, idempotency_key) DO NOTHING RETURNING id"
            ),
            {"w": workspace_id, "a": account_id, "b": build_id, "k": kind, "n": amount, "key": key},
        )
    ).scalar_one_or_none()
    return inserted is not None


async def reserve_build(
    db: AsyncSession, workspace_id: UUID, account: Account, build_id: UUID, cap: int
) -> None:
    """Caller holds the account row lock (current_account(lock=True)) and checked availability."""
    if cap == 0:
        return
    if not await _entry(db, workspace_id, account.id, build_id, "reserve", cap, f"build:{build_id}:reserve"):
        return
    await db.execute(
        text(
            "UPDATE usage_accounts SET reserved_micro_usd = reserved_micro_usd + :n, version = version + 1"
            " WHERE workspace_id = :w AND id = :a"
        ),
        {"n": cap, "w": workspace_id, "a": account.id},
    )
    await db.execute(
        text("UPDATE builds SET reserved_micro_usd = :n WHERE workspace_id = :w AND id = :b"),
        {"n": cap, "w": workspace_id, "b": build_id},
    )


async def settle_attempt(
    db: AsyncSession, workspace_id: UUID, build_id: UUID, attempt_id: UUID, amount: int
) -> int:
    """Charge an attempt's actual cost: reserved -> settled on the build and its account, once.
    The earmark is released implicitly (the attempt is no longer open). Returns the amount charged."""
    build = (
        await db.execute(
            text(
                "SELECT usage_account_id, reserved_micro_usd FROM builds WHERE workspace_id = :w AND id = :b FOR UPDATE"
            ),
            {"w": workspace_id, "b": build_id},
        )
    ).one()
    if amount <= 0:
        return 0
    charged = min(amount, int(build.reserved_micro_usd))  # the earmark bound makes this a no-op cap
    if build.usage_account_id is None:
        # An own-key build (M6, T24): the provider bills the workspace's own account, so only the build's
        # envelope moves; no allowance account and no ledger entry. Replay builds reserve nothing.
        if charged:
            await db.execute(
                text(
                    "UPDATE builds SET reserved_micro_usd = reserved_micro_usd - :n,"
                    " settled_micro_usd = settled_micro_usd + :n WHERE workspace_id = :w AND id = :b"
                ),
                {"n": charged, "w": workspace_id, "b": build_id},
            )
        return charged
    inserted = (
        await db.execute(
            text(
                "INSERT INTO usage_entries(workspace_id, account_id, build_id, attempt_id, kind, amount_micro_usd,"
                " idempotency_key) VALUES (:w, :a, :b, :t, 'settle', :n, :key)"
                " ON CONFLICT (workspace_id, idempotency_key) DO NOTHING RETURNING id"
            ),
            {
                "w": workspace_id,
                "a": build.usage_account_id,
                "b": build_id,
                "t": attempt_id,
                "n": charged,
                "key": f"attempt:{attempt_id}:settle",
            },
        )
    ).scalar_one_or_none()
    if inserted is None:
        return 0
    await _move(db, workspace_id, build.usage_account_id, build_id, reserved=-charged, settled=charged)
    return charged


async def unresolved_holds(db: AsyncSession, workspace_id: UUID, build_id: UUID) -> int:
    """Earmarks of provider attempts that are still running or whose outcome is unknown (T11)."""
    held = (
        await db.execute(
            text(
                "SELECT COALESCE(sum(max_reserved_micro_usd), 0) FROM provider_attempts"
                " WHERE workspace_id = :w AND build_id = :b AND state IN ('started', 'batched', 'unknown')"
            ),
            {"w": workspace_id, "b": build_id},
        )
    ).scalar_one()
    return int(held)


async def holds_in_flight(db: AsyncSession, workspace_id: UUID, build_id: UUID) -> int:
    """Earmarks of attempts still running (`started`) or waiting on a provider batch (`batched`): they
    settle soon and free their room, unlike `unknown` holds."""
    held = (
        await db.execute(
            text(
                "SELECT COALESCE(sum(max_reserved_micro_usd), 0) FROM provider_attempts"
                " WHERE workspace_id = :w AND build_id = :b AND state IN ('started', 'batched')"
            ),
            {"w": workspace_id, "b": build_id},
        )
    ).scalar_one()
    return int(held)


async def release_build(db: AsyncSession, workspace_id: UUID, build_id: UUID) -> int:
    """Return the unspent envelope of a terminal build to its account. Returns the released amount."""
    build = (
        await db.execute(
            text(
                "SELECT usage_account_id, reserved_micro_usd FROM builds"
                " WHERE workspace_id = :w AND id = :b FOR UPDATE"
            ),
            {"w": workspace_id, "b": build_id},
        )
    ).one()
    if build.usage_account_id is None or build.reserved_micro_usd == 0:
        return 0
    amount = int(build.reserved_micro_usd) - await unresolved_holds(db, workspace_id, build_id)
    if amount <= 0:
        return 0
    # Keyed by the remaining amount so a later release (after an unknown outcome resolves) is distinct.
    key = f"build:{build_id}:release:{build.reserved_micro_usd}"
    if not await _entry(db, workspace_id, build.usage_account_id, build_id, "release", amount, key):
        return 0
    await _move(db, workspace_id, build.usage_account_id, build_id, reserved=-amount, settled=0)
    return amount


STALE_HOLD_HOURS = 24  # an unknown outcome older than this is settled at its maximum
TERMINAL_STATUSES = ("ready", "failed", "cancelled", "budget_exhausted")


async def settle_stale_hold(db: AsyncSession, workspace_id: UUID, build_id: UUID, attempt_id: UUID) -> int:
    """Settle one unresolved attempt (unknown, or still `started` long after any call could run) at its
    maximum once it is STALE_HOLD_HOURS old: the provider may have billed it, and the maximum is the most
    it could have cost (07 "Maximum cost"). The attempt becomes `reconciled`; a terminal build then
    releases whatever else it still holds. Returns the amount settled; 0 when already resolved."""
    build = (
        await db.execute(
            text("SELECT status FROM builds WHERE workspace_id = :w AND id = :b FOR UPDATE"),
            {"w": workspace_id, "b": build_id},
        )
    ).one()  # lock order everywhere: the build row first
    maximum = (
        await db.execute(
            text(
                "UPDATE provider_attempts SET state = 'reconciled', settled_micro_usd = max_reserved_micro_usd,"
                " error_code = COALESCE(error_code, 'unresolved'), completed_at = COALESCE(completed_at, now())"
                " WHERE workspace_id = :w AND build_id = :b AND id = :a AND state IN ('started', 'unknown')"
                " AND created_at < now() - make_interval(hours => :h) RETURNING max_reserved_micro_usd"
            ),
            {"w": workspace_id, "b": build_id, "a": attempt_id, "h": STALE_HOLD_HOURS},
        )
    ).scalar_one_or_none()
    if maximum is None:
        return 0
    charged = await settle_attempt(db, workspace_id, build_id, attempt_id, int(maximum))
    if build.status in TERMINAL_STATUSES:
        await release_build(db, workspace_id, build_id)
    return charged


async def _move(
    db: AsyncSession, workspace_id: UUID, account_id: UUID, build_id: UUID, *, reserved: int, settled: int
) -> None:
    await db.execute(
        text(
            "UPDATE usage_accounts SET reserved_micro_usd = reserved_micro_usd + :r,"
            " settled_micro_usd = settled_micro_usd + :s, version = version + 1 WHERE workspace_id = :w AND id = :a"
        ),
        {"r": reserved, "s": settled, "w": workspace_id, "a": account_id},
    )
    await db.execute(
        text(
            "UPDATE builds SET reserved_micro_usd = reserved_micro_usd + :r, settled_micro_usd = settled_micro_usd + :s"
            " WHERE workspace_id = :w AND id = :b"
        ),
        {"r": reserved, "s": settled, "w": workspace_id, "b": build_id},
    )
