"""File validation (US-14): the rules, the isolated PDF probe, the ClamAV client and the local
object store. The malware test signature and the bombs are built in memory at runtime: nothing
hostile is ever committed or written to disk."""

import asyncio
import hashlib
import io
import struct
import subprocess  # nosec B404 - only CompletedProcess, to stand in for the probe
import threading
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from gateway.adapters.files.clamav import ClamAV
from gateway.adapters.files.pdf import PdfProbe
from gateway.adapters.storage.local import LocalObjectStore
from gateway.domain.errors import DomainError, PayloadTooLarge, Unavailable
from gateway.domain.files import (
    FileLimits,
    FileRejected,
    _eicar,
    check_image,
    check_size,
    clean_filename,
    image_size,
    inspect_office,
    resolve_type,
    scan,
    sniff,
)

LIMITS = FileLimits()


# ------------------------------------------------------------------------- test file builders


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + pixels + chunk(b"IEND", b"")


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + bytes(3)
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + bytes(10)


def webp(width: int, height: int) -> bytes:
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    vp8x = b"VP8X" + struct.pack("<I", 10) + bytes(4) + canvas
    return b"RIFF" + struct.pack("<I", 4 + len(vp8x)) + b"WEBP" + vp8x


def pdf(pages: int = 1, *, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner-only", algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def text_pdf(*texts: str) -> bytes:
    """A PDF whose pages carry the given text layer (Helvetica), built by hand."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(texts)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(texts)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(texts):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + 2 * i)
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    trailer = b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
    out.write(trailer % (len(objects) + 1, xref))
    return out.getvalue()


def office(kind: str = "docx", *, slides: int = 0, extra: dict[str, bytes] | None = None) -> bytes:
    main = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(main[kind], "<document/>")
        for i in range(slides):
            archive.writestr(f"ppt/slides/slide{i + 1}.xml", "<slide/>")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return out.getvalue()


def bomb(megabytes: int = 4) -> bytes:
    """A docx whose one media member is zeros: about 1000:1, far beyond any real document."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        with archive.open("word/media/zeros.bin", "w") as member:
            for _ in range(megabytes):
                member.write(bytes(1 << 20))
    return out.getvalue()


def _code(exc: pytest.ExceptionInfo[DomainError]) -> str:
    return exc.value.code


# ----------------------------------------------------------------------------------- the rules


def test_the_type_comes_from_the_content_and_must_match_the_extension() -> None:
    assert resolve_type("report.pdf", sniff(pdf())) == ("pdf", "application/pdf")
    assert resolve_type("Notes.TXT", sniff(b"hello")) == ("text", "text/plain")
    assert resolve_type("deck.docx", sniff(office()))[0] == "docx"
    cases = {
        ("photo.pdf", sniff(png(4, 4))): "file_type_mismatch",
        ("report.png", sniff(pdf())): "file_type_mismatch",
        ("archive.docx", sniff(b"\x7fELF" + bytes(64))): "file_type_mismatch",
        ("tool.exe", sniff(b"MZ" + bytes(64))): "unsupported_file_type",
        ("README", "text"): "unsupported_file_type",
        ("locked.docx", sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(64))): "encrypted_file",
    }
    for (name, sniffed), code in cases.items():
        with pytest.raises(FileRejected) as exc:
            resolve_type(name, sniffed)
        assert _code(exc) == code, name
        assert exc.value.message


def test_names_with_paths_or_hidden_characters_are_refused() -> None:
    assert clean_filename("  report.pdf ") == "report.pdf"
    spoofed = "invoice" + chr(0x202E) + "fdp.exe"  # a right-to-left override hides ".exe"
    for name in ("../../etc/passwd.txt", "a\\b.txt", spoofed, "tab\tname.txt", "", None, "x" * 256):
        with pytest.raises(FileRejected) as exc:
            clean_filename(name)
        assert _code(exc) == "invalid_filename"


def test_size_limits() -> None:
    check_size(1, LIMITS)
    with pytest.raises(FileRejected) as empty:
        check_size(0, LIMITS)
    assert _code(empty) == "empty_file"
    with pytest.raises(PayloadTooLarge) as big:
        check_size(LIMITS.max_bytes + 1, LIMITS)
    assert big.value.code == "file_too_large"
    assert "25.0 MiB" in big.value.message


def test_the_scan_hashes_and_finds_the_malware_test_signature_across_chunk_edges() -> None:
    data = b"plain text " * 1000
    assert scan(io.BytesIO(data), text=True) == hashlib.sha256(data).digest()
    straddling = b"a" * ((1 << 16) - 10) + _eicar() + b" tail"
    for text in (True, False):
        with pytest.raises(FileRejected) as exc:
            scan(io.BytesIO(straddling), text=text)
        assert _code(exc) == "malware_detected"


def test_text_must_be_utf8_without_nul_bytes() -> None:
    for bad in (b"ok\x00ok", b"\xff\xfe broken", b"caf\xc3"):
        with pytest.raises(FileRejected) as exc:
            scan(io.BytesIO(bad), text=True)
        assert _code(exc) == "file_type_mismatch"


@pytest.mark.parametrize(
    ("kind", "build"), [("png", png), ("jpeg", jpeg), ("gif", gif), ("webp", webp)]
)
def test_image_sizes_come_from_the_header(kind: str, build: Callable[[int, int], bytes]) -> None:
    assert sniff(build(640, 480)) == kind
    assert image_size(kind, build(640, 480)) == (640, 480)
    check_image(kind, build(640, 480), LIMITS)
    with pytest.raises(FileRejected) as exc:
        check_image(kind, build(10_000, 10_000), LIMITS)
    assert _code(exc) == "image_too_large"


def test_an_unreadable_image_header_is_a_damaged_file() -> None:
    with pytest.raises(FileRejected) as exc:
        check_image("png", b"\x89PNG\r\n\x1a\n" + bytes(8), LIMITS)
    assert _code(exc) == "corrupt_file"


def test_office_files_are_checked_without_trusting_their_directory() -> None:
    assert inspect_office(io.BytesIO(office()), "docx", LIMITS) is None
    assert inspect_office(io.BytesIO(office("pptx", slides=3)), "pptx", LIMITS) == 3
    many = {f"m{i}.xml": b"<m/>" for i in range(5)}
    cases = [
        (office("pptx", slides=3), "pptx", FileLimits(max_pages=2), "too_many_pages"),
        (bomb(), "docx", LIMITS, "archive_bomb"),  # the compression ratio
        (
            office(extra={"a.bin": bytes(range(256)) * 8}),
            "docx",
            FileLimits(max_unpacked_bytes=1024),
            "archive_bomb",
        ),
        (office(extra=many), "docx", FileLimits(max_entries=4), "archive_bomb"),
        (office("xlsx"), "docx", LIMITS, "file_type_mismatch"),
        (office(extra={"word/media/x.txt": _eicar()}), "docx", LIMITS, "malware_detected"),
        (b"PK\x03\x04" + bytes(200), "docx", LIMITS, "corrupt_file"),
    ]
    for data, kind, limits, code in cases:
        with pytest.raises(FileRejected) as exc:
            inspect_office(io.BytesIO(data), kind, limits)
        assert _code(exc) == code


# ------------------------------------------------------------------------- the isolated probe


async def test_the_pdf_probe_counts_pages_and_refuses_locked_damaged_and_long_files() -> None:
    probe = PdfProbe(timeout_s=60)
    assert await probe.pages(io.BytesIO(pdf(3)), LIMITS) == 3
    assert await probe.pages(io.BytesIO(pdf(2, password="")), LIMITS) == 2  # opens without one
    cases = [
        (pdf(1, password="s3cret"), LIMITS, "encrypted_file"),
        (b"%PDF-1.7\n" + b"\x00garbage" * 64, LIMITS, "corrupt_file"),
        (pdf(3), FileLimits(max_pages=2), "too_many_pages"),
    ]
    for data, limits, code in cases:
        with pytest.raises(FileRejected) as exc:
            await probe.pages(io.BytesIO(data), limits)
        assert _code(exc) == code


async def test_a_probe_past_its_deadline_is_killed_and_the_file_refused() -> None:
    with pytest.raises(FileRejected) as exc:
        await PdfProbe(timeout_s=0.001).pages(io.BytesIO(pdf()), LIMITS)
    assert _code(exc) == "unprocessable_file"


def _one_page(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(argv, 0, stdout=b'{"pages": 1}')


async def test_the_probe_gets_none_of_the_gateway_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:pw@db/finops")
    seen: list[dict[str, str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(kwargs["env"])
        return _one_page(argv)

    monkeypatch.setattr(subprocess, "run", run)
    assert await PdfProbe(timeout_s=5).pages(io.BytesIO(pdf()), LIMITS) == 1
    assert len(seen) == 1
    assert set(seen[0]) <= {"SYSTEMROOT"}  # Windows needs it to start Python; nothing else


async def test_a_cancelled_upload_keeps_its_probe_slot_until_the_probe_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    runs: list[int] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        runs.append(1)
        release.wait(10)
        return _one_page(argv)

    monkeypatch.setattr(subprocess, "run", run)
    probe = PdfProbe(timeout_s=10, concurrency=1)
    try:
        first = asyncio.create_task(probe.pages(io.BytesIO(pdf()), LIMITS))
        for _ in range(500):
            if runs:
                break
            await asyncio.sleep(0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(probe.pages(io.BytesIO(pdf()), LIMITS))
        await asyncio.sleep(0.2)
        assert len(runs) == 1  # the abandoned probe still holds the only slot
    finally:
        release.set()
    assert await second == 1
    assert len(runs) == 2


# ---------------------------------------------------------------------------------- ClamAV


async def test_the_clamav_client_speaks_instream_and_fails_closed() -> None:
    async def clamd(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readexactly(10) == b"zINSTREAM\0"
        data = b""
        while size := int.from_bytes(await reader.readexactly(4), "big"):
            data += await reader.readexactly(size)
        writer.write(b"stream: Eicar-Signature FOUND\0" if _eicar() in data else b"stream: OK\0")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(clamd, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        scanner = ClamAV(f"127.0.0.1:{port}", timeout_s=5)
        await scanner.check(io.BytesIO(b"clean " * 50_000))
        with pytest.raises(FileRejected) as found:
            await scanner.check(io.BytesIO(b"x" + _eicar()))
        assert _code(found) == "malware_detected"
    finally:
        server.close()
        await server.wait_closed()
    with pytest.raises(Unavailable) as down:
        await ClamAV(f"127.0.0.1:{port}", timeout_s=2).check(io.BytesIO(b"clean"))
    assert down.value.code == "scanner_unavailable"


# ---------------------------------------------------------------------------- local storage


async def test_the_local_store_round_trips_and_never_leaves_its_root(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    assert await store.is_ready()
    await store.put("orgs/a1/files/b2", io.BytesIO(b"hello " * 30_000), content_type="text/plain")
    chunks = await store.stream("orgs/a1/files/b2")
    assert b"".join([c async for c in chunks]) == b"hello " * 30_000
    assert not list((tmp_path / "orgs" / "a1" / "files").glob(".*"))  # no partial files left
    await store.delete("orgs/a1/files/b2")
    await store.delete("orgs/a1/files/b2")  # already gone: not an error
    with pytest.raises(FileNotFoundError):
        await store.stream("orgs/a1/files/b2")
    for key in ("../escape", "orgs/../../x", "/abs", "Upper", "a//b", ""):
        with pytest.raises(ValueError, match="invalid object key"):
            await store.put(key, io.BytesIO(b"x"), content_type="text/plain")
