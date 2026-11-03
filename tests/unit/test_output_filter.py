"""The streamed output guardrail: no part of a secret or PII value is ever released, the same
value keeps its placeholder across streams, and ordinary text is not held back for long.
Key-shaped strings are assembled at runtime so secret scanners never see them in the source."""

import json
import time

from gateway.pipeline.guardrails import OutputFilter
from tests.unit.test_detectors import _private_key

KEY = "AK" + "IA" + "QWERTYUIOPASDFGH"
EVERYTHING = frozenset({"pii", "secret"})


def _stream(text: str, *, size: int = 1) -> tuple[list[str], OutputFilter]:
    """Feed `text` in pieces of `size` characters; returns what came out of each feed and the
    final flush (last), and the filter."""
    out = OutputFilter(EVERYTHING)
    stream = out.stream()
    released = [stream.feed(text[i : i + size]) for i in range(0, len(text), size)]
    return [*released, stream.flush()], out


def test_a_secret_split_across_chunks_is_never_released() -> None:
    released, out = _stream(f"Use {KEY} to deploy. Then rest.")
    so_far = ""
    for piece in released:
        so_far += piece
        assert "AKIA" not in so_far  # not even a prefix of the key goes out
    assert so_far == "Use [REDACTED_SECRET] to deploy. Then rest."
    assert [(d["rule"], d["count"]) for d in out.decisions()] == [("aws_access_key", 1)]


def test_pii_in_a_long_line_without_punctuation_is_redacted_and_text_still_flows() -> None:
    filler = "word " * 150
    text = f"{filler}contact jane.doe@example.com today {filler}"
    released, _ = _stream(text, size=3)
    assert "".join(released[:-1])  # most of it went out before the stream ended
    whole = "".join(released)
    assert "jane.doe" not in whole
    assert whole == text.replace("jane.doe@example.com", "[EMAIL_1]")


def test_a_private_key_block_never_leaks_line_by_line() -> None:
    pem, lines = _private_key()
    for size in (1, 7, 65):
        released, _ = _stream(f"Here it is:\n{pem}\nDone.\n", size=size)
        whole = "".join(released)
        assert whole == "Here it is:\n[REDACTED_SECRET]\nDone.\n"
        assert not any(line in whole for line in lines)


def test_a_private_key_in_json_tool_arguments_is_redacted_and_the_json_stays_valid() -> None:
    pem, lines = _private_key("")
    released, _ = _stream(json.dumps({"key": pem, "note": "rotate it"}), size=5)
    whole = "".join(released)
    assert not any(line in whole for line in lines)
    assert json.loads(whole) == {"key": "[REDACTED_SECRET]", "note": "rotate it"}


def test_minified_json_streams_before_the_end() -> None:
    payload = json.dumps([{"id": i, "name": f"item{i}"} for i in range(300)], separators=(",", ":"))
    released, _ = _stream(payload, size=4)
    assert len("".join(released[:-1])) > len(payload) // 2
    assert "".join(released) == payload


def test_placeholders_agree_across_the_content_and_tool_calls() -> None:
    out = OutputFilter(EVERYTHING)
    content, arguments = out.stream(), out.stream()
    first = content.feed("mail jane.doe@example.com\n")
    second = arguments.feed('{"to": "jane.doe@example.com"}') + arguments.flush()
    assert first == "mail [EMAIL_1]\n"
    assert json.loads(second) == {"to": "[EMAIL_1]"}


def test_streaming_cost_stays_linear() -> None:
    started = time.perf_counter()
    _stream("A sentence with nothing to hide. " * 6_000, size=3)  # about 200 KB
    _stream("x" * 100_000, size=3)  # one enormous token: cut at the hard limit
    assert time.perf_counter() - started < 3.0
