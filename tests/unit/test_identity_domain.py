"""US-01 domain rules: email normalisation, password policy, slugs, RBAC scoping."""

from uuid import uuid7

import pytest

from gateway.domain.errors import InvalidInput
from gateway.domain.identity import normalize_email, password_problems, slugify, validate_slug
from gateway.domain.policy import Permission, Principal, Role, RoleGrant

CATALOG = {
    "org_admin": frozenset(p.value for p in Permission),
    "member": frozenset({"models:use", "usage:read_self"}),
    "team_lead": frozenset({"budgets:manage_member", "api_keys:manage"}),
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Jane.Doe@Example.COM ", "jane.doe@example.com"),
        ("a+b@sub.example.io", "a+b@sub.example.io"),
    ],
)
def test_emails_are_normalised(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw", ["no-at-sign", "a@b", "a@-bad.com", "a b@example.com", "x" * 65 + "@ex.com"]
)
def test_invalid_emails_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidInput) as err:
        normalize_email(raw)
    assert err.value.code == "invalid_email"


def test_strong_password_has_no_problems() -> None:
    assert password_problems("Correct-Horse-Battery-9!", email="jane@example.com") == []


@pytest.mark.parametrize(
    ("password", "fragment"),
    [
        ("Sh0rt!", "at least 12 characters"),
        ("x" * 129 + "Ab1", "at most 128 characters"),
        ("Password1234", "commonly used"),
        ("abababababab", "6 different characters"),
        ("janedoe-rocks-2026", "email name"),
        ("acmecorp-rules-2026", "organization name"),
    ],
)
def test_weak_passwords_report_each_problem(password: str, fragment: str) -> None:
    problems = password_problems(password, email="janedoe@example.com", context=("AcmeCorp",))
    assert any(fragment in p for p in problems), problems


@pytest.mark.parametrize(
    ("name", "slug"),
    [("Acme Corp", "acme-corp"), ("  Ünïcödé Ltd!! ", "unicode-ltd"), ("!!!", "org")],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug
    assert validate_slug(slug) == slug


@pytest.mark.parametrize("slug", ["-bad", "bad-", "UPPER", "a" * 64, "sp ace", ""])
def test_invalid_slugs_are_rejected(slug: str) -> None:
    with pytest.raises(InvalidInput):
        validate_slug(slug)


def _principal(*grants: RoleGrant) -> Principal:
    return Principal(uuid7(), uuid7(), "u@example.com", grants, CATALOG)


def test_authorization_is_deny_by_default() -> None:
    assert not _principal().can(Permission.MODELS_USE)
    assert not _principal(RoleGrant(Role.MEMBER)).can(Permission.BUDGETS_MANAGE_MEMBER)


def test_org_grants_apply_to_every_team() -> None:
    admin = _principal(RoleGrant(Role.ORG_ADMIN))
    assert admin.can(Permission.BUDGETS_MANAGE_MEMBER, uuid7())
    assert admin.can(Permission.TEAMS_MANAGE)


def test_team_grants_apply_only_to_their_own_team() -> None:
    team, other = uuid7(), uuid7()
    lead = _principal(RoleGrant(Role.MEMBER), RoleGrant(Role.TEAM_LEAD, team))
    assert lead.can(Permission.BUDGETS_MANAGE_MEMBER, team)
    assert not lead.can(Permission.BUDGETS_MANAGE_MEMBER, other)
    assert not lead.can(Permission.BUDGETS_MANAGE_MEMBER)  # org-level action
    assert lead.can_somewhere(Permission.BUDGETS_MANAGE_MEMBER)
    assert lead.led_team_ids() == {team}
