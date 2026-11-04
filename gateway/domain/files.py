"""File validation rules (spec section 4, stage 5; US-14).

A file's type comes from its content, never from its name or the client's Content-Type, and it
must agree with the name's extension. Office files are ZIP archives: every member is decompressed
under hard caps (entries, total size, compression ratio), so a bomb is refused before anything
trusts its directory. Names are checked but never used to build a storage path. Everything here
is pure: callers hand in bytes or a readable, seekable binary file.
"""

import codecs
import hashlib
import re
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from typing import BinaryIO, Final

from gateway.domain.errors import InvalidInput, PayloadTooLarge

_MIB: Final = 1 << 20
_CHUNK: Final = 1 << 16
SNIFF_BYTES: Final = _MIB  # enough for any image header, JPEGs with large EXIF blocks included
PURPOSES: Final = frozenset({"user_data", "assistants", "vision", "batch", "fine-tune", "evals"})
OFFICE: Final = frozenset({"docx", "xlsx", "pptx"})
IMAGES: Final = frozenset({"png", "jpeg", "gif", "webp"})
_OOXML: Final = "application/vnd.openxmlformats-officedocument."
_BY_EXTENSION: Final[dict[str, tuple[str, str]]] = {
    ".pdf": ("pdf", "application/pdf"),
    ".png": ("png", "image/png"),
    ".jpg": ("jpeg", "image/jpeg"),
    ".jpeg": ("jpeg", "image/jpeg"),
    ".gif": ("gif", "image/gif"),
    ".webp": ("webp", "image/webp"),
    ".docx": ("docx", _OOXML + "wordprocessingml.document"),
    ".xlsx": ("xlsx", _OOXML + "spreadsheetml.sheet"),
    ".pptx": ("pptx", _OOXML + "presentationml.presentation"),
    ".txt": ("text", "text/plain"),
    ".md": ("text", "text/markdown"),
    ".csv": ("text", "text/csv"),
    ".json": ("text", "application/json"),
    ".html": ("text", "text/html"),
    ".htm": ("text", "text/html"),
}
_MAIN_PART: Final = {
    "docx": "word/document.xml",
    "xlsx": "xl/workbook.xml",
    "pptx": "ppt/presentation.xml",
}
_SLIDE: Final = re.compile(r"ppt/slides/slide\d+\.xml")
_CONTENT: Final = {
    "pdf": "a PDF document",
    "png": "a PNG image",
    "jpeg": "a JPEG image",
    "gif": "a GIF image",
    "webp": "a WebP image",
    "zip": "a ZIP archive",
    "cfb": "an encrypted or legacy Office file",
    "text": "plain text",
}


def _eicar() -> bytes:
    """The EICAR anti-malware test signature (https://www.eicar.org), joined at runtime so that
    no file on disk, source or bytecode, ever holds it whole."""
    return b"".join(
        (b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$", b"EICAR-STANDARD-ANTIVIRUS", b"-TEST-FILE!$H+H*")
    )


_EICAR: Final = _eicar()


class FileRejected(InvalidInput):
    """A file refused for a reason its uploader can act on; `code` names the reason."""

    default_code = "file_rejected"


def _reject(message: str, code: str) -> FileRejected:
    return FileRejected(message, code=code, param="file")


@dataclass(frozen=True, slots=True)
class FileLimits:
    max_bytes: int = 25 * _MIB
    max_pages: int = 500  # PDF pages or presentation slides
    max_image_pixels: int = 50_000_000
    max_unpacked_bytes: int = 200 * _MIB  # every member of an Office file together
    max_entries: int = 5_000
    max_ratio: int = 250  # uncompressed to compressed size, for members over 1 MiB


def clean_filename(raw: str | None) -> str:
    """The uploaded name, NFC-normalised. Names with a path, or with control or format characters
    (a right-to-left override can disguise an extension), are refused rather than rewritten, so
    the uploader learns that the name was the problem."""
    name = unicodedata.normalize("NFC", raw or "").strip()
    if not 1 <= len(name) <= 255:
        raise _reject("the file name must be 1 to 255 characters long", "invalid_filename")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise _reject("the file name must not contain a path", "invalid_filename")
    if any(unicodedata.category(c).startswith("C") for c in name):
        raise _reject("the file name contains control or invisible characters", "invalid_filename")
    return name


def check_size(size: int, limits: FileLimits) -> None:
    if size == 0:
        raise _reject("the file is empty", "empty_file")
    if size > limits.max_bytes:
        raise PayloadTooLarge(
            f"the file is {size / _MIB:.1f} MiB; the limit is {limits.max_bytes / _MIB:.1f} MiB",
            code="file_too_large",
            param="file",
        )


def sniff(head: bytes) -> str | None:
    """The kind of content from its first bytes: an accepted kind, "zip" (every Office file),
    "cfb" (an encrypted or legacy Office file), or None."""
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "cfb"
    return "text" if _looks_like_text(head) else None


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:  # a character cut by the end of the sample is fine
        return exc.reason == "unexpected end of data" and exc.start >= len(head) - 3
    return True


def resolve_type(filename: str, sniffed: str | None) -> tuple[str, str]:
    """(kind, MIME type) of a file whose content sniffed as `sniffed`; the name's extension must
    agree with the content."""
    dot = filename.rfind(".")
    ext = filename[dot:].lower() if dot > 0 else ""
    if ext not in _BY_EXTENSION:
        what = f"{ext} files" if ext else "files without an extension"
        accepted = " ".join(sorted(_BY_EXTENSION))
        raise _reject(f"{what} are not accepted; accepted: {accepted}", "unsupported_file_type")
    kind, mime = _BY_EXTENSION[ext]
    if kind in OFFICE and sniffed == "cfb":
        raise _reject(
            "password-protected Office files are not accepted; remove the password and retry",
            "encrypted_file",
        )
    if sniffed != ("zip" if kind in OFFICE else kind):
        content = _CONTENT.get(sniffed or "", "of no accepted type")
        raise _reject(
            f"the file's content is {content}, which does not match its {ext} extension",
            "file_type_mismatch",
        )
    return kind, mime


def scan(file: BinaryIO, *, text: bool) -> bytes:
    """One pass over the whole file: its SHA-256, the malware test signature (also across chunk
    edges) and, for text, valid UTF-8 without NUL bytes. Returns the digest."""
    file.seek(0)
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")() if text else None
    tail = b""
    while chunk := file.read(_CHUNK):
        digest.update(chunk)
        tail = _check_signature(tail, chunk)
        if decoder is not None:
            _check_text(decoder, chunk)
    if decoder is not None:
        _check_text(decoder, b"", final=True)
    file.seek(0)
    return digest.digest()


def _check_signature(tail: bytes, chunk: bytes) -> bytes:
    """Refuse the malware test signature; returns the tail to prepend to the next chunk."""
    window = tail + chunk
    if _EICAR in window:
        raise _reject("the file contains a malware signature and was refused", "malware_detected")
    return window[-(len(_EICAR) - 1) :]


def _check_text(decoder: codecs.IncrementalDecoder, chunk: bytes, *, final: bool = False) -> None:
    try:
        if b"\x00" in chunk:
            raise UnicodeDecodeError("utf-8", chunk, 0, 1, "NUL byte")
        decoder.decode(chunk, final)
    except UnicodeDecodeError as exc:
        raise _reject("the file is not valid UTF-8 text", "file_type_mismatch") from exc


def check_image(kind: str, head: bytes, limits: FileLimits) -> None:
    size = image_size(kind, head)
    if size is None or 0 in size:
        raise _reject("the image header cannot be read; the file may be damaged", "corrupt_file")
    width, height = size
    if width * height > limits.max_image_pixels:
        raise _reject(
            f"the image is {width}x{height} pixels; the limit is"
            f" {limits.max_image_pixels / 1e6:g} megapixels",
            "image_too_large",
        )


def image_size(kind: str, head: bytes) -> tuple[int, int] | None:
    """Width and height from the image header, without decoding the image; None if unreadable."""
    try:
        if kind == "png" and head[12:16] == b"IHDR":
            width, height = struct.unpack(">II", head[16:24])
            return width, height
        if kind == "gif":
            width, height = struct.unpack("<HH", head[6:10])
            return width, height
        if kind == "webp":
            return _webp_size(head)
        if kind == "jpeg":
            return _jpeg_size(head)
    except struct.error, IndexError:
        return None
    return None


def _webp_size(head: bytes) -> tuple[int, int] | None:
    if len(head) < 30:
        return None
    chunk = head[12:16]
    if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":  # lossy: after the frame tag
        width, height = struct.unpack("<HH", head[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and head[20] == 0x2F:  # lossless: 14-bit sizes, minus one
        bits = int.from_bytes(head[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":  # extended: 24-bit canvas sizes, minus one
        width = int.from_bytes(head[24:27], "little") + 1
        return width, int.from_bytes(head[27:30], "little") + 1
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG segments to the first frame header (SOFn)."""
    i = 2
    while i + 9 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # a fill byte
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:  # markers without a length
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    return None


def inspect_office(file: BinaryIO, kind: str, limits: FileLimits) -> int | None:
    """Check an Office file without trusting its directory: the entry count is read from the
    archive's end record before anything else, sizes and compression ratios are capped, and every
    member is decompressed under those caps and scanned for the malware test signature. Returns
    the slide count of a presentation (None for documents and spreadsheets)."""
    entries = _declared_entries(file)
    if entries is not None and entries > limits.max_entries:
        raise _reject(
            f"the archive has {entries} entries; the limit is {limits.max_entries}",
            "archive_bomb",
        )
    try:
        file.seek(0)
        with zipfile.ZipFile(file) as archive:
            members = archive.infolist()
            _check_directory(members, kind, limits)
            budget = limits.max_unpacked_bytes
            for member in members:
                budget -= _unpack(archive, member, budget)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, OSError) as exc:
        raise _reject("the file is damaged and cannot be read", "corrupt_file") from exc
    finally:
        file.seek(0)
    if kind != "pptx":
        return None
    slides = sum(1 for m in members if _SLIDE.fullmatch(m.filename))
    if slides > limits.max_pages:
        raise _reject(
            f"the presentation has {slides} slides; the limit is {limits.max_pages}",
            "too_many_pages",
        )
    return slides


def _declared_entries(file: BinaryIO) -> int | None:
    """The entry count in the end-of-central-directory record (Zip64 aware), read before
    zipfile builds one object per entry; None when there is no such record."""
    file.seek(0, 2)
    size = file.tell()
    file.seek(max(0, size - 65_557))  # the record (22 bytes) plus the longest comment
    tail = file.read()
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or at + 22 > len(tail):
        return None
    count = int.from_bytes(tail[at + 10 : at + 12], "little")
    locator = tail.rfind(b"PK\x06\x07", 0, at)
    if count == 0xFFFF and locator >= 0:  # Zip64: the real count is in the Zip64 end record
        file.seek(int.from_bytes(tail[locator + 8 : locator + 16], "little"))
        record = file.read(56)
        if record[:4] == b"PK\x06\x06":
            count = int.from_bytes(record[32:40], "little")
    return count


def _check_directory(members: list[zipfile.ZipInfo], kind: str, limits: FileLimits) -> None:
    if len(members) > limits.max_entries:
        raise _reject(
            f"the archive has {len(members)} entries; the limit is {limits.max_entries}",
            "archive_bomb",
        )
    names = {m.filename for m in members}
    if "[Content_Types].xml" not in names or _MAIN_PART[kind] not in names:
        raise _reject(f"the file is a ZIP archive, not a {kind} document", "file_type_mismatch")
    if any(m.flag_bits & 0x1 for m in members):
        raise _reject(
            "password-protected Office files are not accepted; remove the password and retry",
            "encrypted_file",
        )
    declared = sum(m.file_size for m in members)
    if declared > limits.max_unpacked_bytes:
        raise _reject(
            f"the file unpacks to {declared / _MIB:.0f} MiB; the limit is"
            f" {limits.max_unpacked_bytes / _MIB:.0f} MiB",
            "archive_bomb",
        )
    for m in members:
        if m.file_size > _MIB and m.file_size > limits.max_ratio * max(m.compress_size, 1):
            raise _reject(
                "the file is compressed far beyond any real document and was refused",
                "archive_bomb",
            )


def _unpack(archive: zipfile.ZipFile, member: zipfile.ZipInfo, budget: int) -> int:
    """Decompress one member under what is left of the budget. zipfile stops at the member's
    declared size and checks its CRC, so a lying header surfaces as a damaged file."""
    size, tail = 0, b""
    with archive.open(member) as stream:
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if size > budget:
                raise _reject("the file unpacks to more than the limit allows", "archive_bomb")
            tail = _check_signature(tail, chunk)
    return size
