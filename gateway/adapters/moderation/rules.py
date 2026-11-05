"""Built-in moderation rules (US-24): the categories image providers refuse, as patterns. A
conservative baseline that needs no service; MODERATION_BACKEND=openai_compat puts a moderation
model in its place. Each pattern is searched on its own, so the check stays linear in the prompt."""

import re
from typing import Final

from gateway.domain.images import Moderation

_MINOR: Final = re.compile(
    r"\b(child|children|kid|kids|minor|minors|underage|preteen|toddler|teen|teens|teenager"
    r"|schoolgirl|schoolboy)\b",
    re.IGNORECASE,
)
_SEXUAL: Final = re.compile(
    r"\b(nude|nudes|naked|sexual|sexy|erotic|porn\w*|lingerie|explicit|nsfw|genitals?"
    r"|hardcore)\b",
    re.IGNORECASE,
)
_RULES: Final = (
    (
        "violence/graphic",
        re.compile(
            r"\b(gore|gory|dismember\w*|decapitat\w*|disembowel\w*|mutilat\w*|beheading)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "self-harm",
        re.compile(
            r"\b(suicide|self[- ]harm|slit(?:ting)? (?:my|her|his|their) wrists?)\b",
            re.IGNORECASE,
        ),
    ),
    ("hate", re.compile(r"\b(nazi propaganda|white power|ethnic cleansing)\b", re.IGNORECASE)),
)


class RulesModerator:
    name = "rules"

    async def check(self, text: str) -> Moderation:
        categories = [category for category, pattern in _RULES if pattern.search(text)]
        if _SEXUAL.search(text):
            categories.append("sexual/minors" if _MINOR.search(text) else "sexual")
        return Moderation(bool(categories), tuple(categories))
