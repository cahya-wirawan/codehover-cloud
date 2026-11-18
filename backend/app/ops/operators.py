"""`python -m app.ops.operators grant|revoke|list`: the platform operations role (09).

Operators are granted by the deployment, outside the product: this command uses the DDL role
(`MIGRATION_DATABASE_URL`), because no application role may write the `operators` table. A grant names
an existing user by ID (shown on their account page and in audit logs) and a short note.

    python -m app.ops.operators grant <user-id> "on-call rotation"
    python -m app.ops.operators revoke <user-id>
    python -m app.ops.operators list
"""

from __future__ import annotations

import asyncio
import json
import sys
from uuid import UUID

from sqlalchemy import text

USAGE = 'usage: python -m app.ops.operators grant <user-id> "<note>" | revoke <user-id> | list'


async def run(argv: list[str]) -> int:
    from app.core.db import create_engine
    from app.core.settings import get_settings

    settings = get_settings()
    if settings.migration_database_url is None:
        print("MIGRATION_DATABASE_URL (DDL role) is required", file=sys.stderr)
        return 2
    engine = create_engine(
        settings, url=settings.migration_database_url.get_secret_value(), application_name="operators"
    )
    try:
        async with engine.begin() as conn:
            if argv[:1] == ["list"] and len(argv) == 1:
                rows = await conn.execute(
                    text(
                        "SELECT user_id, granted_at, note FROM operators WHERE revoked_at IS NULL ORDER BY granted_at"
                    )
                )
                print(
                    json.dumps(
                        [
                            {
                                "user_id": str(r.user_id),
                                "granted_at": r.granted_at.isoformat(),
                                "note": r.note,
                            }
                            for r in rows
                        ]
                    )
                )
                return 0
            if argv[:1] == ["grant"] and len(argv) == 3:
                user = UUID(argv[1])
                found = (
                    await conn.execute(text("SELECT status FROM users WHERE id = :u"), {"u": user})
                ).scalar_one_or_none()
                if found != "active":
                    print("no active user with that ID", file=sys.stderr)
                    return 1
                await conn.execute(
                    text(
                        "INSERT INTO operators(user_id, note) VALUES (:u, :n) ON CONFLICT (user_id)"
                        " DO UPDATE SET revoked_at = NULL, note = EXCLUDED.note, granted_at = now()"
                    ),
                    {"u": user, "n": argv[2][:200]},
                )
                await conn.execute(
                    text(
                        "INSERT INTO operator_events(operator_user_id, action, target_id, reason)"
                        " VALUES (:u, 'operator.granted', :u, :n)"
                    ),
                    {"u": user, "n": argv[2][:500]},
                )
                print(json.dumps({"granted": str(user)}))
                return 0
            if argv[:1] == ["revoke"] and len(argv) == 2:
                user = UUID(argv[1])
                revoked = (
                    await conn.execute(
                        text(
                            "UPDATE operators SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"
                            " RETURNING user_id"
                        ),
                        {"u": user},
                    )
                ).one_or_none()
                if revoked is None:
                    print("not an operator", file=sys.stderr)
                    return 1
                await conn.execute(
                    text(
                        "INSERT INTO operator_events(operator_user_id, action, target_id)"
                        " VALUES (:u, 'operator.revoked', :u)"
                    ),
                    {"u": user},
                )
                print(json.dumps({"revoked": str(user)}))
                return 0
    except ValueError:
        print("not a user ID", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:])))
