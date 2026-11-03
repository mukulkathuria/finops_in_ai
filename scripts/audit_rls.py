"""Audit: every tenant table has forced RLS and a tenant policy (spec section 12).

A table is a tenant table when it has an `org_id` column (or is `orgs`). Any other table must be
listed in GLOBAL_TABLES, which catches tenant data that forgot its `org_id`.

Usage: AUDIT_DATABASE_URL=postgresql://... python scripts/audit_rls.py   (exit 1 on violations)
"""

import os
import sys

import psycopg

GLOBAL_TABLES = frozenset(
    {
        "alembic_version",
        "roles",
        "permissions",
        "role_permissions",
        "system_state",
        "rate_limit_counters",  # hashed client keys only, no tenant data
        "model_catalog",  # global price list, read-only for runtime roles
    }
)
OWNER_ROLE = "migrator"
RUNTIME_ROLES = ("app", "worker", "readonly_analytics")

_TABLES = """
SELECT c.oid, c.relname, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner),
       EXISTS (SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = c.oid AND a.attname = 'org_id' AND NOT a.attisdropped)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
 ORDER BY c.relname
"""
_POLICIES = """
SELECT polcmd, pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid)
  FROM pg_policy WHERE polrelid = %s AND polname = 'tenant_isolation'
"""
_PARTITIONS = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relispartition
   AND (has_table_privilege(%s, c.oid, 'SELECT') OR has_table_privilege(%s, c.oid, 'INSERT'))
"""


def find_violations(conn: psycopg.Connection) -> list[str]:
    problems: list[str] = []
    for oid, name, rls, forced, owner, has_org in conn.execute(_TABLES).fetchall():
        if name in GLOBAL_TABLES:
            continue
        if owner != OWNER_ROLE:
            problems.append(f"{name}: owned by {owner}, expected {OWNER_ROLE}")
        if not (has_org or name == "orgs"):
            problems.append(f"{name}: no org_id column and not listed in GLOBAL_TABLES")
            continue
        if not rls or not forced:
            problems.append(f"{name}: row level security enabled={rls} forced={forced}")
        policy = conn.execute(_POLICIES, (oid,)).fetchone()
        if policy is None:
            problems.append(f"{name}: missing tenant_isolation policy")
            continue
        cmd, using, check = policy
        if (
            cmd != "*"
            or "app_current_org()" not in (using or "")
            or "app_current_org()" not in (check or "")
        ):
            problems.append(f"{name}: tenant_isolation must cover ALL with USING and WITH CHECK")
    for role in RUNTIME_ROLES:
        for (part,) in conn.execute(_PARTITIONS, (role, role)).fetchall():
            problems.append(f"{part}: partition directly accessible by {role}")
    return problems


def main() -> int:
    url = os.environ.get("AUDIT_DATABASE_URL") or os.environ.get("MIGRATOR_DATABASE_URL")
    if not url:
        print("set AUDIT_DATABASE_URL", file=sys.stderr)
        return 2
    with psycopg.connect(url) as conn:
        problems = find_violations(conn)
    for p in problems:
        print(f"RLS VIOLATION: {p}")
    print(f"rls audit: {len(problems)} violation(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
