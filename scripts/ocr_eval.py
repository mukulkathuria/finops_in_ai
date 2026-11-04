"""OCR evaluation (spec section 3A, "Choosing the backend"; Phase 2 exit criteria).

Runs a labelled set of documents through each OCR backend's parser sidecar and reports, as
Markdown, the character and word error rates, page confidence and time per backend, language and
document type, then names the backend to set as OCR_BACKEND.

    uv run python scripts/ocr_eval.py --docs eval/ocr \\
        --backend glm_ocr=http://glm-ocr:8000/v1 --model glm_ocr=zai-org/GLM-OCR \\
        --backend paddleocr_vl=http://paddleocr-vl:8080/v1 \\
        --model paddleocr_vl=PaddleOCR-VL-1.5-0.9B --out docs/reports/ocr-eval.md

The set: every document (PDF, PNG, JPEG or WebP) has its ground truth beside it as <name>.gt.md;
an optional manifest.json maps file names to {"language": "de", "type": "invoice"}. Pages are
read with no confidence bar, so every page's text counts.
"""

import argparse
import asyncio
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx2

MIMES: Final = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_MARKUP: Final = re.compile(r"[#*_`|>]+|^\s*[-+]\s+|-{3,}", re.MULTILINE)
_SPACE: Final = re.compile(r"\s+")


@dataclass(frozen=True)
class Sample:
    path: Path
    truth: str
    language: str
    kind: str


@dataclass(frozen=True)
class Result:
    backend: str
    sample: Sample
    cer: float = 1.0
    wer: float = 1.0
    confidence: float = 0.0
    seconds: float = 0.0
    error: str | None = None


def normalize(text: str) -> str:
    """Markdown syntax and spacing are not part of what was read; letters and their case are."""
    return _SPACE.sub(" ", _MARKUP.sub(" ", unicodedata.normalize("NFKC", text))).strip()


def distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Levenshtein distance (insertions, deletions, substitutions) in two rows of memory."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def error_rates(truth: str, read: str) -> tuple[float, float]:
    """(character error rate, word error rate) of `read` against `truth`."""
    t, r = normalize(truth), normalize(read)
    words = t.split()
    return distance(t, r) / max(len(t), 1), distance(words, r.split()) / max(len(words), 1)


def load(directory: Path) -> list[Sample]:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}
    samples = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in MIMES:
            continue
        truth = path.with_name(path.stem + ".gt.md")
        if not truth.exists():
            raise SystemExit(f"no ground truth for {path.name} (expected {truth.name})")
        meta = manifest.get(path.name, {})
        samples.append(
            Sample(
                path,
                truth.read_text("utf-8"),
                str(meta.get("language", "unknown")),
                str(meta.get("type", "unknown")),
            )
        )
    if not samples:
        raise SystemExit(f"no documents in {directory}")
    return samples


async def evaluate(
    client: httpx2.AsyncClient, backend: str, url: str, model: str | None, sample: Sample
) -> Result:
    options = {"backend": backend, "model": model, "min_confidence": 0.0}
    started = time.perf_counter()
    try:
        resp = await client.post(
            f"{url.rstrip('/')}/parse",
            files={
                "file": (
                    sample.path.name,
                    sample.path.read_bytes(),
                    MIMES[sample.path.suffix.lower()],
                )
            },
            data={"options": json.dumps(options)},
            timeout=600,
        )
        resp.raise_for_status()
        body = resp.json()
        pages = body["pages"]
        cer, wer = error_rates(sample.truth, str(body["markdown"]))
        confidence = sum(float(p["confidence"]) for p in pages) / max(len(pages), 1)
    except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
        return Result(backend, sample, error=f"{type(exc).__name__}: {exc}"[:200])
    return Result(backend, sample, cer, wer, confidence, time.perf_counter() - started)


async def evaluate_all(
    client: httpx2.AsyncClient,
    samples: Sequence[Sample],
    backends: dict[str, str],
    models: dict[str, str],
) -> list[Result]:
    """Every document through every backend, one at a time, so timings stay comparable."""
    return [
        await evaluate(client, name, url, models.get(name), sample)
        for name, url in backends.items()
        for sample in samples
    ]


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _table(rows: dict[tuple[str, ...], list[Result]], heads: Sequence[str]) -> list[str]:
    lines = [
        "| "
        + " | ".join([*heads, "Documents", "CER", "WER", "Confidence", "Seconds", "Failed"])
        + " |",
        "| " + " | ".join(["---"] * (len(heads) + 6)) + " |",
    ]
    for key in sorted(rows):
        done = [r for r in rows[key] if r.error is None]
        lines.append(
            "| "
            + " | ".join(
                [
                    *key,
                    str(len(rows[key])),
                    f"{_mean(r.cer for r in done):.2%}",
                    f"{_mean(r.wer for r in done):.2%}",
                    f"{_mean(r.confidence for r in done):.2f}",
                    f"{_mean(r.seconds for r in done):.1f}",
                    str(len(rows[key]) - len(done)),
                ]
            )
            + " |"
        )
    return lines


def report(results: Sequence[Result]) -> str:
    by_backend: dict[tuple[str, ...], list[Result]] = defaultdict(list)
    by_language: dict[tuple[str, ...], list[Result]] = defaultdict(list)
    by_type: dict[tuple[str, ...], list[Result]] = defaultdict(list)
    for r in results:
        by_backend[(r.backend,)].append(r)
        by_language[(r.backend, r.sample.language)].append(r)
        by_type[(r.backend, r.sample.kind)].append(r)
    scored = {
        key[0]: _mean(r.cer for r in group if r.error is None)
        for key, group in by_backend.items()
        if any(r.error is None for r in group)
    }
    lines = [
        "# OCR evaluation",
        "",
        f"Run {datetime.now(UTC):%Y-%m-%d %H:%M} UTC on {len({r.sample.path for r in results})}"
        " documents. CER and WER are edit distances against the ground truth, after Markdown"
        " syntax and spacing are set aside (lower is better).",
        "",
        "## By backend",
        "",
        *_table(by_backend, ["Backend"]),
        "",
        "## By language",
        "",
        *_table(by_language, ["Backend", "Language"]),
        "",
        "## By document type",
        "",
        *_table(by_type, ["Backend", "Type"]),
        "",
        "## Failures",
        "",
        *(
            [f"- {r.backend}, {r.sample.path.name}: {r.error}" for r in results if r.error]
            or ["None."]
        ),
        "",
        "## Recommendation",
        "",
    ]
    if scored:
        best = min(scored, key=lambda name: scored[name])
        lines.append(
            f"Set `OCR_BACKEND={best}`: it has the lowest mean character error rate"
            f" ({scored[best]:.2%}). Check the language and type tables for documents where"
            " the other backend is better before deciding."
        )
    else:
        lines.append("No backend read any document; see the failures above.")
    return "\n".join(lines) + "\n"


def _pairs(values: Sequence[str], flag: str) -> dict[str, str]:
    pairs = {}
    for value in values:
        name, sep, rest = value.partition("=")
        if not sep or not name or not rest:
            raise SystemExit(f"{flag} expects name=value, got {value!r}")
        pairs[name] = rest
    return pairs


async def run(argv: Sequence[str]) -> str:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--docs", type=Path, required=True)
    parser.add_argument("--backend", action="append", required=True, help="name=sidecar URL")
    parser.add_argument("--model", action="append", default=[], help="name=model name")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    backends, models = _pairs(args.backend, "--backend"), _pairs(args.model, "--model")
    samples = load(args.docs)
    async with httpx2.AsyncClient() as client:
        results = await evaluate_all(client, samples, backends, models)
    text = report(results)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    sys.stdout.write(asyncio.run(run(sys.argv[1:])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
