"""The RLS audit's rule for policies beside tenant_isolation (scripts/audit_rls.py): only a hash
lookup or the worker's. Permissive policies are OR-ed, so any other one widens access."""

import pytest

from scripts.audit_rls import extra_policy_problem


@pytest.mark.parametrize(
    ("cmd", "using", "roles"),
    [
        ("r", "(key_hash = app_lookup('key_hash'::text))", ["public"]),  # a pre-tenant lookup
        ("*", "true", ["worker"]),  # the cross-tenant role
        ("r", "true", ["worker"]),
    ],
)
def test_lookups_and_the_workers_policies_pass(cmd: str, using: str, roles: list[str]) -> None:
    assert extra_policy_problem("t", "p", cmd, using, roles) is None


@pytest.mark.parametrize(
    ("cmd", "using", "roles"),
    [
        ("r", "true", ["app"]),  # every row, for the runtime role
        ("r", "true", ["public"]),  # every row, for everyone
        ("*", "(key_hash = app_lookup('key_hash'::text))", ["public"]),  # a lookup that writes
        ("r", "(org_id IS NOT NULL)", ["public"]),  # not a lookup at all
        ("r", "true", ["app", "worker"]),  # the worker's, shared with the runtime role
    ],
)
def test_any_other_policy_is_a_violation(cmd: str, using: str, roles: list[str]) -> None:
    assert extra_policy_problem("t", "p", cmd, using, roles) is not None
