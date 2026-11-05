"""The code indexer's parser in a process of its own (US-22), isolated like the PDF probe: a JSON
source bundle comes in on stdin and the outlines of its Python modules go out on stdout as JSON.
Nothing is executed. The process bounds the memory and the time a hostile bundle can take, and
the gateway's event loop never waits on the parser."""

import importlib
import sys
from dataclasses import asdict
from typing import Final

import orjson

from gateway.adapters.code.python_ast import outline_all

_MEMORY: Final = 2 * 1024**3  # bytes of address space, where the platform can limit it


def _limit_memory() -> None:
    try:
        resource = importlib.import_module("resource")  # POSIX only
    except ImportError:
        return
    resource.setrlimit(resource.RLIMIT_AS, (_MEMORY, _MEMORY))


def main() -> int:
    _limit_memory()
    try:
        files = orjson.loads(sys.stdin.buffer.read())["files"]
        if not isinstance(files, dict):
            raise TypeError("`files` maps paths to sources")
        found = outline_all(files)
    except (KeyError, TypeError, ValueError) as exc:  # JSONDecodeError is a ValueError
        sys.stdout.buffer.write(orjson.dumps({"error": str(exc)[:300]}))
        return 0
    answer = {"modules": [asdict(m) for m in found.modules], "skipped": list(found.skipped)}
    sys.stdout.buffer.write(orjson.dumps(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
