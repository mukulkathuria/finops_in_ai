"""Input guardrails (US-11, US-12): a request holding more text than the guardrails read is refused
before any of it is scanned (the checks cost about two seconds of CPU per million characters)."""

from uuid import uuid4

import pytest

from gateway.domain.errors import PayloadTooLarge
from gateway.domain.guardrails import GuardrailPolicy
from gateway.domain.llm import CanonicalRequest, Message, TextPart
from gateway.pipeline.guardrails import Guardrails


class _Untouched:
    async def get(self, org_id: object) -> GuardrailPolicy:
        raise AssertionError("an oversized request must be refused before anything is read")


async def test_a_request_with_more_text_than_the_limit_is_refused_before_it_is_read() -> None:
    guardrails = Guardrails(
        classifier=None, policies=_Untouched(), telemetry=None, max_input_chars=10_000
    )
    text = TextPart("x" * 10_001)
    big = CanonicalRequest(model="m", messages=(Message("user", (text,)),), max_tokens=5)
    with pytest.raises(PayloadTooLarge) as exc:
        await guardrails.inspect_chat(uuid4(), big, request_id="r")
    assert (exc.value.code, exc.value.kind) == ("input_too_large", "too_large")
    with pytest.raises(PayloadTooLarge):
        await guardrails.inspect_inputs(uuid4(), ["x" * 6_000, "y" * 6_000], request_id="r")
