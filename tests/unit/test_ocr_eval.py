"""The OCR evaluation harness: edit distances, Markdown-insensitive scoring, and a full run against
stand-in sidecars (one exact, one sloppy, one failing on a document)."""

import json
from pathlib import Path

import httpx2
import pytest

from scripts.ocr_eval import distance, error_rates, evaluate_all, load, report


def test_edit_distance() -> None:
    assert distance("kitten", "sitting") == 3
    assert distance("", "abc") == distance("abc", "") == 3
    assert distance(["a", "b"], ["a", "c", "b"]) == 1


def test_scores_ignore_markdown_syntax_but_not_letters() -> None:
    assert error_rates("# Invoice\n\n**Total:** 42 EUR", "Invoice Total: 42 EUR") == (0.0, 0.0)
    cer, wer = error_rates("Total 42", "Total 43")
    assert cer == pytest.approx(1 / 8)
    assert wer == pytest.approx(1 / 2)
    assert error_rates("Case", "case")[0] > 0  # a wrong case is a wrong character


async def test_a_run_reports_by_backend_language_and_type(tmp_path: Path) -> None:
    truths = {"invoice.pdf": "# Invoice\n\nTotal: 42 EUR", "letter.png": "Dear Ana, thank you."}
    for name, truth in truths.items():
        (tmp_path / name).write_bytes(b"not decoded by the stand-in")
        (tmp_path / (Path(name).stem + ".gt.md")).write_text(truth, encoding="utf-8")
    manifest = {
        "invoice.pdf": {"language": "en", "type": "invoice"},
        "letter.png": {"language": "es"},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    samples = load(tmp_path)
    assert [s.kind for s in samples] == ["invoice", "unknown"]

    def sidecar(request: httpx2.Request) -> httpx2.Response:
        options = json.loads(dict(_fields(request))["options"])
        name = _filename(request)
        if options["backend"] == "flaky" and name == "letter.png":
            return httpx2.Response(503)
        text = truths[name] if options["backend"] == "exact" else truths[name].upper()
        page = {"page": 1, "method": "ocr", "confidence": 0.9, "markdown": text}
        return httpx2.Response(200, json={"markdown": text, "pages": [page]})

    backends = {
        "exact": "http://exact/v1",
        "sloppy": "http://sloppy/v1",
        "flaky": "http://flaky/v1",
    }
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(sidecar)) as client:
        results = await evaluate_all(client, samples, backends, {"exact": "m"})
    text = report(results)
    assert "| exact | 2 | 0.00% | 0.00% | 0.90 |" in text
    assert "| exact | es |" in text
    assert "| sloppy | invoice |" in text
    assert "flaky, letter.png: HTTPStatusError" in text
    assert "Set `OCR_BACKEND=exact`" in text


def _fields(request: httpx2.Request) -> list[tuple[str, str]]:
    """The text fields of a multipart body (enough for this stand-in)."""
    body = request.read().decode("latin-1")
    fields = []
    for part in body.split("--")[1:]:
        if 'name="options"' in part:
            fields.append(("options", part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0]))
    return fields


def _filename(request: httpx2.Request) -> str:
    body = request.read().decode("latin-1")
    return body.split('filename="', 1)[1].split('"', 1)[0]
