"""Lossless whitespace compaction: the first compression technique (US-26).

Trailing spaces, runs of spaces inside a line and runs of blank lines carry no meaning in prose,
so they go. Indentation and fenced code blocks can carry meaning, so they stay as written. Every
step is linear in the text.
"""

import re
from typing import Final

_FENCED: Final = re.compile(r"^(?:```|~~~).*?^(?:```|~~~)[^\n]*$", re.MULTILINE | re.DOTALL)
_INNER_RUN: Final = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")
_BLANK_RUN: Final = re.compile(r"\n{3,}")


def _prose(text: str) -> str:
    lines = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", _INNER_RUN.sub(" ", lines))


def compact(text: str) -> str:
    """The text without redundant whitespace, fenced code blocks left exactly as they were."""
    out: list[str] = []
    last = 0
    for block in _FENCED.finditer(text):
        out += (_prose(text[last : block.start()]), block.group())
        last = block.end()
    out.append(_prose(text[last:]))
    return "".join(out)
