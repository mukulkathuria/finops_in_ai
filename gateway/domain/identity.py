"""Identity rules: email normalisation, password policy, org slugs (US-01). Pure logic."""

import re
import unicodedata
from typing import Final

from gateway.domain.errors import InvalidInput

MIN_PASSWORD_LENGTH: Final = 12
MAX_PASSWORD_LENGTH: Final = 128
MIN_DISTINCT_CHARS: Final = 6

_EMAIL: Final = re.compile(
    r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_SLUG: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NOT_SLUG: Final = re.compile(r"[^a-z0-9]+")

# Frequently breached passwords that pass the length rule. Lower-cased comparison.
_COMMON_PASSWORDS: Final = frozenset(
    {
        "123456789012",
        "1234567890123",
        "12345678901234",
        "123456789abc",
        "abc123456789",
        "qwertyuiop123",
        "qwertyuiopasdf",
        "qwerty123456",
        "1q2w3e4r5t6y",
        "1q2w3e4r5t6y7u",
        "1qaz2wsx3edc",
        "zaq12wsxcde3",
        "qazwsxedcrfv",
        "asdfghjkl123",
        "zxcvbnm12345",
        "password1234",
        "password12345",
        "password123456",
        "passwordpassword",
        "password123!",
        "p@ssw0rd1234",
        "passw0rd1234",
        "iloveyou1234",
        "letmein12345",
        "letmeinplease",
        "welcome12345",
        "welcome123456",
        "admin1234567",
        "administrator",
        "changeme1234",
        "trustno1trustno1",
        "football1234",
        "baseball1234",
        "superman1234",
        "princess1234",
        "sunshine1234",
        "monkey123456",
        "dragon123456",
        "master123456",
        "shadow123456",
        "michael12345",
        "correcthorsebatterystaple",
        "aaaaaaaaaaaa",
        "111111111111",
        "000000000000",
        "abcdefghijkl",
        "abcdefabcdef",
    }
)


def normalize_email(raw: str) -> str:
    email = unicodedata.normalize("NFKC", raw).strip().lower()
    if len(email) > 320 or not _EMAIL.fullmatch(email):
        raise InvalidInput("email address is not valid", code="invalid_email", param="email")
    return email


def password_problems(
    password: str, *, email: str = "", context: tuple[str, ...] = ()
) -> list[str]:
    """Every rule the password breaks, as user-facing phrases. Empty means acceptable."""
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(f"must be at most {MAX_PASSWORD_LENGTH} characters")
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS:
        problems.append("is a commonly used password")
    if len(set(password)) < MIN_DISTINCT_CHARS:
        problems.append(f"must contain at least {MIN_DISTINCT_CHARS} different characters")
    local_part = email.split("@", 1)[0]
    if len(local_part) >= 4 and local_part in lowered:
        problems.append("must not contain your email name")
    if any(len(w) >= 4 and w in lowered for w in (c.strip().lower() for c in context)):
        problems.append("must not contain the organization name")
    return problems


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = _NOT_SLUG.sub("-", ascii_name.lower()).strip("-")[:63].strip("-")
    return slug or "org"


def validate_slug(slug: str) -> str:
    if not _SLUG.fullmatch(slug):
        raise InvalidInput(
            "slug must be 1-63 lowercase letters, digits or inner hyphens",
            code="invalid_slug",
            param="org_slug",
        )
    return slug
