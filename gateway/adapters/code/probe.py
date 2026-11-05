"""Runs the outline probe (outline_probe.py) for the code indexer: a process per bundle, with
none of the gateway's environment, killed at its deadline, and at most `concurrency` at once. The
probe runs through `subprocess.run` in a worker thread, as the PDF probe does, so it works on
every event loop."""

import asyncio
import os
import subprocess  # nosec B404 - runs only this interpreter on the bundled probe script
import sys
from pathlib import Path
from typing import Any, Final

import orjson

from gateway.domain.code import Module, Symbol
from gateway.domain.errors import InvalidInput

_PROBE: Final = str(Path(__file__).with_name("outline_probe.py"))


def _env() -> dict[str, str]:
    """None of the gateway's environment (database URL, keys) reaches the probe; Windows only
    needs SYSTEMROOT to start Python."""
    root = os.environ.get("SYSTEMROOT")
    return {"SYSTEMROOT": root} if root else {}


def _run(bundle: bytes, timeout_s: float) -> bytes:
    done = subprocess.run(  # noqa: S603  # nosec B603 - a fixed argv, no shell
        [sys.executable, "-I", _PROBE],
        input=bundle,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout_s,
        check=False,
    )
    return done.stdout


def _module(raw: dict[str, Any]) -> Module:
    symbols = tuple(
        Symbol(s["name"], s["kind"], s["signature"], s["doc"], tuple(s["calls"]))
        for s in raw["symbols"]
    )
    return Module(
        raw["path"],
        raw["name"],
        raw["doc"],
        tuple(raw["imports"]),
        symbols,
        int(raw["source_tokens"]),
    )


class OutlineProbe:
    def __init__(self, *, timeout_s: float = 120.0, concurrency: int = 2) -> None:
        self._timeout = timeout_s
        self._slots = asyncio.Semaphore(concurrency)  # bounds probe processes and their memory

    async def outlines(self, bundle: bytes) -> tuple[list[Module], list[str]]:
        """The bundle's module outlines and the files left out, or InvalidInput."""
        async with self._slots:
            try:
                out = await asyncio.to_thread(_run, bundle, self._timeout)
            except subprocess.TimeoutExpired as exc:  # `run` has already killed the probe
                raise InvalidInput(
                    "the bundle took too long to index", code="bundle_too_slow"
                ) from exc
        try:
            answer = orjson.loads(out)
            if "error" in answer:
                raise InvalidInput(
                    f"not a source bundle: {answer['error']}", code="invalid_code_bundle"
                )
            modules = [_module(m) for m in answer["modules"]]
            return modules, [str(s) for s in answer["skipped"]]
        except (KeyError, TypeError, ValueError) as exc:  # no answer: killed, or out of memory
            raise InvalidInput(
                "the bundle could not be indexed; it may be too large", code="invalid_code_bundle"
            ) from exc
