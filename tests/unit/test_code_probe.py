"""US-22 review S-M2: Python sources are parsed in a process of their own, with a deadline and
no gateway environment, so a hostile bundle can neither exhaust the gateway's memory nor stall
its event loop."""

import orjson
import pytest

from gateway.adapters.code.probe import OutlineProbe
from gateway.domain.errors import InvalidInput

CART = '"""The cart."""\ndef total(items: list[int]) -> int:\n    return sum(items)\n'


async def test_sources_are_outlined_in_a_separate_process() -> None:
    bundle = {"files": {"shop/cart.py": CART, "notes.txt": "hi", "shop/../../x.py": CART}}
    modules, skipped = await OutlineProbe().outlines(orjson.dumps(bundle))
    assert [(m.path, m.name, m.doc) for m in modules] == [
        ("shop/cart.py", "shop.cart", "The cart.")
    ]
    assert modules[0].symbols[0].signature == "def total(items: list[int]) -> int"
    assert modules[0].symbols[0].calls == ("sum",)
    assert sorted(skipped) == ["notes.txt", "shop/../../x.py"]


async def test_a_bundle_past_its_deadline_or_malformed_is_refused() -> None:
    with pytest.raises(InvalidInput, match="too long") as slow:
        await OutlineProbe(timeout_s=0.001).outlines(orjson.dumps({"files": {}}))
    assert slow.value.code == "bundle_too_slow"
    with pytest.raises(InvalidInput, match="not a source bundle"):
        await OutlineProbe().outlines(b'{"files": 5}')
    with pytest.raises(InvalidInput, match="not a source bundle"):
        await OutlineProbe().outlines(b"not json")


def test_a_stopped_child_never_starts_and_a_running_one_is_killed() -> None:
    """Review N-L1: a cancelled call stops its probe instead of leaving it to its deadline."""
    from gateway.adapters.code.probe import _Child

    early = _Child()
    early.stop()
    assert early.start() is None
    child = _Child()
    proc = child.start()
    assert proc is not None
    with proc:  # the probe waits for its bundle on stdin
        child.stop()
        assert proc.wait(timeout=10) != 0


async def test_an_oversized_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review N-M1: the gateway never parses an answer past its bound."""
    from gateway.adapters.code import probe

    monkeypatch.setattr(probe, "_MAX_ANSWER", 10)
    with pytest.raises(InvalidInput, match="too large"):
        await OutlineProbe().outlines(orjson.dumps({"files": {"shop/cart.py": CART}}))
