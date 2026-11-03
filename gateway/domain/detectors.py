"""Deterministic detectors (spec section 3, family C `rules`; US-11, US-12).

PII and secrets are found by pattern plus checksum (Luhn, IBAN mod-97, Verhoeff for Aadhaar,
SSN area rules); prompt injection by canonicalised phrase patterns, including base64-encoded,
leetspeak and a few non-English forms. Deterministic checks always run and always win;
classifiers only advise. Every pattern is linear (bounded repetition, no ambiguous nesting), so
hostile input cannot trigger catastrophic backtracking.
"""

import base64
import binascii
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final, Literal

RULES_VERSION: Final = "rules-1.0"
Category = Literal["pii", "secret"]

_INVISIBLE: Final = re.compile("[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")


def strip_invisible(text: str) -> str:
    """Remove zero-width and soft-hyphen characters: they carry no meaning but split patterns."""
    return _INVISIBLE.sub("", text)


def normalize(text: str) -> str:
    """NFKC (fullwidth digits and letters, compatibility forms) without invisible characters, so
    obfuscated PII, secrets and injections meet the same patterns as plain ones."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))


# Unicode tag characters (U+E0020..U+E007E) mirror ASCII but render as nothing: a known way to
# smuggle instructions to a model past the people reading the text.
_TAG_RUN: Final = re.compile("[\U000e0020-\U000e007e]+")
_UNTAG: Final = {cp: cp - 0xE0000 for cp in range(0xE0020, 0xE007F)}


def reveal_hidden(text: str) -> str:
    """Spell out text hidden in Unicode tag characters as visible words."""
    return _TAG_RUN.sub(lambda m: f" {m.group().translate(_UNTAG)} ", text)


# ---------------------------------------------------------------------------------- checksums


def _digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def luhn_ok(value: str) -> bool:
    digits = _digits(value)
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def iban_ok(value: str) -> bool:
    compact = value.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return int(numeric) % 97 == 1


_VERHOEFF_D: Final = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P: Final = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_ok(value: str) -> bool:
    digits = _digits(value)
    if len(digits) != 12 or len(set(digits)) == 1:
        return False
    check = 0
    for i, ch in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(ch)]]
    return check == 0


def ssn_ok(value: str) -> bool:
    area, group, serial = value.split("-")
    return (
        area not in {"000", "666"}
        and not area.startswith("9")
        and group != "00"
        and serial != "0000"
    )


def _phone_ok(value: str) -> bool:
    return 8 <= len(_digits(value)) <= 15


# ---------------------------------------------------------------------------------- patterns


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str  # e.g. "email", "credit_card", "aws_access_key"
    category: Category
    start: int
    end: int


_Rule = tuple[str, Category, re.Pattern[str], Callable[[str], bool] | None]

# Priority order: secrets first, so an overlapping PII match never hides a secret.
_RULES: Final[tuple[_Rule, ...]] = (
    (
        "private_key",
        "secret",
        # The whole block: header, body (raw or JSON-escaped lines) and the END line if present.
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]{1,12} ){0,3}PRIVATE KEY(?: BLOCK)?-----"
            r"(?:[^-]|-(?!----)){0,16384}"
            r"(?:-----END (?:[A-Z0-9]{1,12} ){0,3}PRIVATE KEY(?: BLOCK)?-----)?"
        ),
        None,
    ),
    (
        "aws_access_key",
        "secret",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b"),
        None,
    ),
    (
        "github_token",
        "secret",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})"),
        None,
    ),
    ("anthropic_key", "secret", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,255}"), None),
    (
        "openai_key",
        "secret",
        re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,255}"),
        None,
    ),
    ("google_api_key", "secret", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), None),
    ("slack_token", "secret", re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,255}"), None),
    ("stripe_key", "secret", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,255}"), None),
    ("finops_api_key", "secret", re.compile(r"\bfo_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{20,255}"), None),
    ("finops_refresh_token", "secret", re.compile(r"\bfrt_[A-Za-z0-9_-]{20,255}"), None),
    (
        "jwt",
        "secret",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,2048}\.eyJ[A-Za-z0-9_-]{8,8192}\.[A-Za-z0-9_-]{8,2048}"),
        None,
    ),
    (
        "email",
        "pii",
        re.compile(
            r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}"
        ),
        None,
    ),
    (
        "iban",
        "pii",
        re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b"),
        iban_ok,
    ),
    # Digit runs are matched whole: never a card or an Aadhaar inside a longer number.
    (
        "credit_card",
        "pii",
        re.compile(r"(?<!\d)(?<!\d[ -])\d(?:[ -]?\d){12,18}(?![ -]?\d)"),
        luhn_ok,
    ),
    ("us_ssn", "pii", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), ssn_ok),
    (
        "aadhaar",
        "pii",
        re.compile(r"(?<!\d)(?<!\d[ -])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![ -]?\d)"),
        verhoeff_ok,
    ),
    ("india_pan", "pii", re.compile(r"\b[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]\b"), None),
    (
        "phone",
        "pii",
        re.compile(
            r"(?<![\w+])\+\d{1,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{2,5}(?:[ .-]?\d{2,5}){1,3}(?!\w)"
        ),
        _phone_ok,
    ),
    (
        "ip_address",
        "pii",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
        ),
        None,
    ),
)
_SECRET_RULES: Final = tuple(r for r in _RULES if r[1] == "secret")
_BASE64_BLOB: Final = re.compile(r"[A-Za-z0-9+/]{24,16384}={0,2}")


def _decode(blob: str) -> str | None:
    """Base64 text that decodes to readable UTF-8, or None."""
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
        text = raw.decode("utf-8")
    except binascii.Error, ValueError, UnicodeDecodeError:
        return None
    printable = sum(c.isprintable() or c.isspace() for c in text)
    return text if text and printable / len(text) > 0.9 else None


def detect(
    text: str, *, categories: frozenset[Category] = frozenset({"pii", "secret"})
) -> list[Finding]:
    """Non-overlapping findings in priority order (secrets win), sorted by position. A base64
    blob that decodes to text holding a secret is itself reported as that secret. Linear in the
    text and in the number of findings: each rule's matches are merged into the claimed spans."""
    taken: list[tuple[int, int]] = []  # spans already claimed: disjoint, sorted
    found: list[Finding] = []
    for rule, category, pattern, valid in _RULES:
        if category not in categories:
            continue
        spans = [
            m.span()
            for m in _unclaimed(pattern.finditer(text), taken)
            if valid is None or valid(m.group())
        ]
        if spans:
            found += (Finding(rule, category, start, end) for start, end in spans)
            taken = sorted(taken + spans)  # two sorted runs: Timsort merges them in linear time
    if "secret" in categories:
        for m in _unclaimed(_BASE64_BLOB.finditer(text), taken):
            decoded = _decode(m.group())
            if decoded is None:
                continue
            for rule, _, pattern, valid in _SECRET_RULES:
                hit = pattern.search(decoded)
                if hit and (valid is None or valid(hit.group())):
                    found.append(Finding(f"{rule}_base64", "secret", m.start(), m.end()))
                    break
    return sorted(found, key=lambda f: f.start)


def _unclaimed(
    matches: Iterator[re.Match[str]], taken: list[tuple[int, int]]
) -> Iterator[re.Match[str]]:
    """The matches (in position order) that overlap no claimed span."""
    i = 0
    for m in matches:
        start, end = m.span()
        while i < len(taken) and taken[i][1] <= start:
            i += 1
        if i == len(taken) or taken[i][0] >= end:
            yield m


class Placeholders:
    """Placeholder numbering shared across a request: the same value always gets the same
    number, so the model can still reason about "[EMAIL_1] wrote to [EMAIL_2]"."""

    def __init__(self) -> None:
        self._numbers: dict[tuple[str, str], int] = {}
        self._counts: dict[str, int] = {}

    def __call__(self, rule: str, value: str) -> str:
        key = (rule, value)
        if key not in self._numbers:
            self._counts[rule] = self._counts.get(rule, 0) + 1
            self._numbers[key] = self._counts[rule]
        return f"[{rule.upper()}_{self._numbers[key]}]"


def redact(text: str, findings: list[Finding], placeholders: Placeholders | None = None) -> str:
    """Replace each finding with its placeholder; secrets become [REDACTED_SECRET]."""
    names = placeholders or Placeholders()
    out: list[str] = []
    cursor = 0
    for f in sorted(findings, key=lambda f: f.start):
        out.append(text[cursor : f.start])
        if f.category == "secret":
            out.append("[REDACTED_SECRET]")
        else:
            out.append(names(f.rule, text[f.start : f.end]))
        cursor = f.end
    out.append(text[cursor:])
    return "".join(out)


# ---------------------------------------------------------------------------------- injection

_LEET: Final = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "@": "a",
        "$": "s",
        "!": "i",
        "|": "l",
    }
)
_NOT_LETTER: Final = re.compile(r"[^0-9a-z\u00c0-\uffff]+")
_W: Final = r"(?: [a-z]{1,20})"  # one ordinary word
_INJECTION_EN: Final = tuple(
    re.compile(p)
    for p in (
        rf"\b(?:ignore|disregard|forget|bypass|override|skip)\b{_W}{{0,3}}"
        r" (?:all|any|the|your|previous|prior|above|earlier|preceding|system|initial|original)\b"
        rf"{_W}{{0,2}}"
        r" (?:instructions?|prompts?|rules?|guidelines?|directives?|guardrails?)\b",
        rf"\b(?:reveal|print|show|repeat|output|leak|dump|display|disclose)\b{_W}{{0,3}}"
        r" (?:system prompt|hidden instructions?|initial instructions?"
        r"|developer (?:message|prompt)|your (?:instructions|prompt|rules))\b",
        rf"\byou are now\b{_W}{{0,3}}"
        r" (?:dan|jailbroken|unfiltered|uncensored|unrestricted|in developer mode|evil)\b",
        r"\b(?:do anything now|developer mode (?:enabled|on)|jailbreak (?:mode|prompt)|dan mode)\b",
        rf"\b(?:act|behave|respond) as\b{_W}{{0,2}}"
        r" (?:unfiltered|uncensored|unrestricted|jailbroken|amoral)\b",
        rf"\bpretend\b{_W}{{0,4}} (?:no|without)\b{_W}{{0,2}}"
        r" (?:rules|restrictions|limits|filters|guidelines)\b",
        rf"\bnew (?:system )?instructions?\b{_W}{{0,2}} (?:override|replace|supersede)\b",
    )
)
_INJECTION_RAW: Final = tuple(
    re.compile(normalize(p), re.IGNORECASE)  # normalised like the text they search
    for p in (
        r"<\|im_start\|>\s*system",
        r"<\|start_header_id\|>\s*system",
        r"<\|system\|>",
        r"\[/?INST\]",
        r"ignora (?:todas )?las instrucciones (?:anteriores|previas)",
        r"ignore[zr]? (?:toutes )?les instructions (?:pr[ée]c[ée]dentes|ant[ée]rieures)",
        r"ignoriere (?:alle )?(?:vorherigen|bisherigen|obigen) anweisungen",
        r"ignore (?:todas )?as instru[çc][õo]es anteriores",
        r"игнорируй(?:те)? (?:все )?(?:предыдущие|прошлые) инструкции",
        r"忽略(?:之前|以上|前面|先前)的(?:所有)?(?:指令|指示|说明|规则)",
        r"पिछले (?:सभी )?निर्देशों को (?:अनदेखा|नज़रअंदाज़|नजरअंदाज) कर",
    )
)


def _canonical(text: str) -> str:
    return _NOT_LETTER.sub(" ", normalize(text).lower().translate(_LEET)).strip()


def _candidates(text: str) -> Iterator[tuple[str, str]]:
    """The text as written, then any text hidden in tag characters, then decoded base64 payloads
    (lazily: most texts match or clear on the first)."""
    yield text, ""
    if (revealed := reveal_hidden(text)) != text:
        yield revealed, "_hidden"
    for m in _BASE64_BLOB.finditer(text):
        if decoded := _decode(m.group()):
            yield decoded, "_base64"


def injection_rule(text: str) -> str | None:
    """The name of the first injection pattern the text matches: as written, hidden in Unicode
    tag characters, or inside a base64 payload."""
    for candidate, suffix in _candidates(text):
        canonical = _canonical(candidate)
        for i, pattern in enumerate(_INJECTION_EN):
            if pattern.search(canonical):
                return f"injection_phrase_{i + 1}{suffix}"
        lowered = normalize(candidate).lower()
        for i, pattern in enumerate(_INJECTION_RAW):
            if pattern.search(lowered):
                return f"injection_marker_{i + 1}{suffix}"
    return None
