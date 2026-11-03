"""Team rules (US-02). Pure logic."""

from typing import Final, Literal

from gateway.domain.errors import InvalidInput

TeamRole = Literal["member", "lead"]
MAX_TEAM_NAME: Final = 100


def clean_team_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not 1 <= len(name) <= MAX_TEAM_NAME:
        raise InvalidInput(
            f"team name must be 1-{MAX_TEAM_NAME} characters",
            code="invalid_team_name",
            param="name",
        )
    return name
