"""Local-disk object store (development, or a single host with a mounted volume). A write lands
in a temporary file beside its target and is renamed into place, so a reader never sees part of
an object."""

import asyncio
import os
import re
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO, Final
from uuid import uuid4

_KEY: Final = re.compile(r"[a-z0-9][a-z0-9/_-]{0,250}")  # no dots: nothing can leave the root
_CHUNK: Final = 1 << 16


class LocalObjectStore:
    name = "local"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        if not _KEY.fullmatch(key) or "//" in key:
            raise ValueError("invalid object key")
        return self._root / key

    async def put(self, key: str, data: BinaryIO, *, content_type: str) -> None:
        await asyncio.to_thread(_write, self._path(key), data)

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        handle = await asyncio.to_thread(self._path(key).open, "rb")
        return _chunks(handle)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def is_ready(self) -> bool:
        return await asyncio.to_thread(_writable, self._root)


def _write(path: Path, data: BinaryIO) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid4().hex}.part")
    try:
        with partial.open("xb") as out:
            data.seek(0)
            shutil.copyfileobj(data, out, _CHUNK * 16)
            out.flush()
            os.fsync(out.fileno())
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
        data.seek(0)


async def _chunks(handle: BinaryIO) -> AsyncIterator[bytes]:
    try:
        while chunk := await asyncio.to_thread(handle.read, _CHUNK):
            yield chunk
    finally:
        handle.close()


def _writable(root: Path) -> bool:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(root, os.W_OK)
