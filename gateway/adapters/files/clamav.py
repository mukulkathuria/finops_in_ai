"""Optional ClamAV scan over clamd's INSTREAM protocol (spec section 4: "optional ClamAV").

When configured, the scan must complete: an unreachable or failing scanner refuses the upload
with 503 (the check fails closed). clamd's StreamMaxLength must be at least MAX_UPLOAD_BYTES.
"""

import asyncio
from typing import BinaryIO, Final

from gateway.domain.errors import Unavailable
from gateway.domain.files import FileRejected

_CHUNK: Final = 1 << 16


class ClamAV:
    def __init__(self, address: str, *, timeout_s: float) -> None:
        host, _, port = address.rpartition(":")
        self._host = host or "localhost"
        self._port = int(port or 3310)
        self._timeout = timeout_s

    async def check(self, file: BinaryIO) -> None:
        try:
            verdict = await asyncio.wait_for(self._scan(file), self._timeout)
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as exc:
            raise Unavailable(
                "the malware scanner is unavailable; try again shortly", code="scanner_unavailable"
            ) from exc
        if verdict.endswith("FOUND"):
            raise FileRejected(
                "the file contains malware and was refused", code="malware_detected", param="file"
            )
        if not verdict.endswith("OK"):
            raise Unavailable(
                "the malware scanner could not scan the file", code="scanner_unavailable"
            )

    async def _scan(self, file: BinaryIO) -> str:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(b"zINSTREAM\0")
            await asyncio.to_thread(file.seek, 0)
            while chunk := await asyncio.to_thread(file.read, _CHUNK):
                writer.write(len(chunk).to_bytes(4, "big") + chunk)
                await writer.drain()
            writer.write(b"\0\0\0\0")
            await writer.drain()
            reply = await reader.readuntil(b"\0")
        finally:
            writer.close()
            await asyncio.to_thread(file.seek, 0)
        return reply.rstrip(b"\0").decode("utf-8", "replace").strip()
