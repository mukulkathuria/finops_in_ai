"""PDF facts for upload validation (US-14), from the isolated probe process in pdf_probe.py.

The probe runs through `subprocess.run` in a worker thread rather than asyncio's subprocess API:
that works on every event loop (psycopg's async mode needs a selector loop, which on Windows
cannot start subprocesses), and `run` kills the probe itself when the deadline passes.
"""

import asyncio
import subprocess  # nosec B404 - runs only this interpreter on the bundled probe script
import sys
from pathlib import Path
from typing import BinaryIO, Final

import orjson

from gateway.domain.files import FileLimits, FileRejected

_PROBE: Final = str(Path(__file__).with_name("pdf_probe.py"))


def _read_all(file: BinaryIO) -> bytes:
    file.seek(0)
    data = file.read()
    file.seek(0)
    return data


def _probe(data: bytes, timeout_s: float) -> bytes:
    """The probe's JSON answer; the PDF goes in on stdin, so it never touches the disk."""
    done = subprocess.run(  # noqa: S603  # nosec B603 - a fixed argv, no shell
        [sys.executable, "-I", _PROBE],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout_s,
        check=False,
    )
    return done.stdout


class PdfProbe:
    def __init__(self, *, timeout_s: float, concurrency: int = 4) -> None:
        self._timeout = timeout_s
        self._slots = asyncio.Semaphore(concurrency)  # bounds probe processes and their memory

    async def pages(self, file: BinaryIO, limits: FileLimits) -> int:
        """The page count of an acceptable PDF; refuses encrypted, damaged and over-long ones."""
        data = await asyncio.to_thread(_read_all, file)
        async with self._slots:
            facts = await self._run(data)
        if facts.get("encrypted"):
            raise FileRejected(
                "password-protected PDFs are not accepted; remove the password and retry",
                code="encrypted_file",
                param="file",
            )
        pages = facts.get("pages")
        if not isinstance(pages, int):
            raise FileRejected(
                "the PDF is damaged and cannot be read", code="corrupt_file", param="file"
            )
        if pages > limits.max_pages:
            raise FileRejected(
                f"the PDF has {pages} pages; the limit is {limits.max_pages}",
                code="too_many_pages",
                param="file",
            )
        return pages

    async def _run(self, data: bytes) -> dict[str, object]:
        # A cancelled request cannot stop the thread, but the probe still dies at its deadline.
        try:
            out = await asyncio.to_thread(_probe, data, self._timeout)
        except subprocess.TimeoutExpired:  # `run` has already killed the probe
            raise FileRejected(
                "the PDF could not be checked in time; it may be damaged or too complex",
                code="unprocessable_file",
                param="file",
            ) from None
        try:
            facts = orjson.loads(out)
        except orjson.JSONDecodeError:  # the probe died (for example at its memory limit)
            return {}
        return facts if isinstance(facts, dict) else {}
