"""Built-in moderation rules (US-24): the categories image providers refuse, as patterns. A
conservative baseline that needs no service; MODERATION_BACKEND=openai_compat puts a moderation
model in its place. Each pattern is searched on its own, so the check stays linear in the prompt.

The patterns meet the prompt as written and folded: compatibility forms and invisible
characters removed, look-alike letters from other scripts and leetspeak mapped to the Latin
letters they imitate, and letters spelled out one at a time ("n u d e", "n.u.d.e") joined."""

import re
from typing import Final

from gateway.domain.detectors import normalize
from gateway.domain.images import Moderation

_LOOK_ALIKES: Final = (
    (0x0430, "a"),  # Cyrillic
    (0x0435, "e"),
    (0x043E, "o"),
    (0x0440, "p"),
    (0x0441, "c"),
    (0x0443, "y"),
    (0x0445, "x"),
    (0x0455, "s"),
    (0x0456, "i"),
    (0x0458, "j"),
    (0x04CF, "l"),
    (0x0501, "d"),
    (0x03B1, "a"),  # Greek
    (0x03B5, "e"),
    (0x03B9, "i"),
    (0x03BA, "k"),
    (0x03BD, "v"),
    (0x03BF, "o"),
    (0x03C1, "p"),
    (0x03C4, "t"),
    (0x03C5, "u"),
)
_LEET: Final = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
_FOLD: Final = str.maketrans({chr(code): latin for code, latin in _LOOK_ALIKES} | _LEET)
_SEPARATORS: Final = re.compile(r"[\W_]+")
_SPELLED: Final = re.compile(r"\b(?:[a-z] ){2,}[a-z]\b")  # three or more single letters


def _spelled(match: re.Match[str]) -> str:
    """Letters spelled one at a time, read as one word, and as one after a lone first letter
    (an article: "a n u d e")."""
    letters = match.group().split()
    return f"{''.join(letters)} {letters[0]} {''.join(letters[1:])}"


def folded(text: str) -> str:
    """The prompt as the patterns should read it, whatever spelling tricks it uses."""
    plain = _SEPARATORS.sub(" ", normalize(text).lower().translate(_FOLD))
    return _SPELLED.sub(_spelled, plain)


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
        readings = (text, folded(text))

        def found(pattern: re.Pattern[str]) -> bool:
            return any(pattern.search(reading) for reading in readings)

        categories = [category for category, pattern in _RULES if found(pattern)]
        if found(_SEXUAL):
            categories.append("sexual/minors" if found(_MINOR) else "sexual")
        return Moderation(bool(categories), tuple(categories))
