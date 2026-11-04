"""Reads a PDF from stdin and prints, as JSON, what upload validation needs to know:
{"pages": n}, {"encrypted": true} or {"error": "<exception name>"}.

The gateway runs this file as `python -I pdf_probe.py` in a short-lived process with memory and
CPU limits and kills it at a deadline: it imports only the standard library and pypdf, so a
hostile PDF can only ever exhaust this process, never the gateway.
"""

import io
import json
import sys

_MEMORY = 1 << 30  # bytes of address space
_CPU_SECONDS = 30

if sys.platform != "win32":
    import resource

    def _limit_resources() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY, _MEMORY))
        resource.setrlimit(resource.RLIMIT_CPU, (_CPU_SECONDS, _CPU_SECONDS))

else:

    def _limit_resources() -> None:
        """Development on Windows: the gateway's deadline still applies."""


def probe(data: bytes) -> dict[str, object]:
    from pypdf import PdfReader  # imported after the limits apply

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):  # an empty password opens some
            return {"encrypted": True}
        return {"pages": len(reader.pages)}
    except Exception as exc:  # noqa: BLE001 - any failure to read means a damaged file
        return {"error": type(exc).__name__}


def main() -> int:
    _limit_resources()
    sys.stdout.write(json.dumps(probe(sys.stdin.buffer.read())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
