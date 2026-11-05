"""Runs the outline probe (outline_probe.py) for the code indexer: a process per bundle, with
none of the gateway's environment, killed at its deadline or when the call is cancelled, and at
most `concurrency` at once. The probe runs through `subprocess` in a worker thread, as the PDF
probe does, so it works on every event loop."""

import asyncio
import contextlib
import os
import subprocess  # nosec B404 - runs only this interpreter on the bundled probe script
import sys
import threading
from pathlib import Path
from typing import Any, Final

import orjson

from gateway.domain.code import Module, Symbol
from gateway.domain.errors import InvalidInput

_PROBE: Final = str(Path(__file__).with_name("outline_probe.py"))
_MAX_ANSWER: Final = 128 * 1024 * 1024  # bytes; a bundle within its limits answers far less


def _env() -> dict[str, str]:
    """None of the gateway's environment (database URL, keys) reaches the probe; Windows only
    needs SYSTEMROOT to start Python."""
    root = os.environ.get("SYSTEMROOT")
    return {"SYSTEMROOT": root} if root else {}


class _Child:
    """One call's probe process, so a cancelled call stops it, or never starts it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._stopped = False

    def start(self) -> subprocess.Popen[bytes] | None:
        with self._lock:
            if self._stopped:
                return None
            self._proc = subprocess.Popen(  # noqa: S603  # nosec B603 - a fixed argv, no shell
                [sys.executable, "-I", _PROBE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_env(),
            )
            return self._proc

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()


def _run(bundle: bytes, timeout_s: float, child: _Child) -> bytes:
    proc = child.start()
    if proc is None:  # cancelled before it started
        return b""
    with proc:
        try:
            out, _ = proc.communicate(bundle, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    return out


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
        child = _Child()
        async with self._slots:
            work = asyncio.ensure_future(asyncio.to_thread(_run, bundle, self._timeout, child))
            try:
                out = await asyncio.shield(work)
            except asyncio.CancelledError:
                child.stop()  # a cancelled call stops its probe; the slot frees once it is gone
                with contextlib.suppress(BaseException):
                    await work
                raise
            except subprocess.TimeoutExpired as exc:  # the probe has been killed
                raise InvalidInput(
                    "the bundle took too long to index", code="bundle_too_slow"
                ) from exc
        if len(out) > _MAX_ANSWER:
            raise InvalidInput("the bundle is too large to index", code="bundle_too_large")
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
