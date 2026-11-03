"""Create users directly (as the superuser) for tests that need several roles in one org."""

from uuid import UUID, uuid7

import psycopg

from tests.support.database import DisposableDatabase


def add_user(
    db: DisposableDatabase,
    org_id: str | UUID,
    *,
    roles: tuple[tuple[str, UUID | None], ...] = (("member", None),),
    email: str | None = None,
) -> UUID:
    user_id = uuid7()
    with psycopg.connect(db.superuser_url) as conn:
        conn.execute(
            "INSERT INTO users (id, org_id, email, status) VALUES (%s, %s, %s, 'active')",
            (user_id, org_id, email or f"{user_id.hex[:12]}@example.com"),
        )
        for role, team_id in roles:
            conn.execute(
                "INSERT INTO role_assignments (org_id, user_id, role, team_id)"
                " VALUES (%s, %s, %s, %s)",
                (org_id, user_id, role, team_id),
            )
    return user_id
