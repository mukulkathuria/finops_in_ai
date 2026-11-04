"""US-15 and US-16: attachments converted to Markdown, and the OCR backend switched by config.

US-15: a text PDF and a scan produce Markdown; tokens before and after are reported; pages read
with low confidence keep the original. US-16: changing OCR_BACKEND switches the backend after a
restart with no code change, and every artifact records the backend that made it. The stand-in
sidecar (tests/support/fake_docparse.py) speaks the parser contract.
"""

from typing import Any

import httpx2
import psycopg
from fastapi import FastAPI

from tests.integration.test_us14_files import _team_key, _upload
from tests.support import fake_docparse
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase
from tests.unit.test_files import pdf, png, text_pdf

Api = tuple[FastAPI, httpx2.AsyncClient]


async def _markdown(client: httpx2.AsyncClient, key: str, file_id: str) -> dict[str, Any]:
    resp = await client.get(f"/v1/files/{file_id}/markdown", headers=bearer(key))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _chat(
    client: httpx2.AsyncClient, key: str, parts: list[dict[str, Any]], role: str = "user"
) -> httpx2.Response:
    return await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={
            "model": "mock/mock-small",
            "messages": [{"role": role, "content": parts}],
            "max_tokens": 200,
        },
    )


def _file(file_id: str) -> dict[str, Any]:
    return {"type": "file", "file": {"file_id": file_id}}


def _flags(db: DisposableDatabase, request_id: str) -> dict[str, Any]:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT flags FROM usage_events WHERE request_id = %s", (request_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0])


async def test_a_text_pdf_and_a_scan_become_markdown_and_report_tokens(api: Api) -> None:
    _, client = api
    key = await _team_key(client, await signup(client))
    report = text_pdf("Revenue grew 12 percent.", "Costs fell 3 percent.")
    text_id = (await _upload(client, key, "report.pdf", report)).json()["id"]
    scan_id = (await _upload(client, key, "scan.pdf", pdf(2))).json()["id"]
    parsed_before = fake_docparse.PARSES.get("glm_ocr", 0)
    text = await _markdown(client, key, text_id)
    assert "Revenue grew 12 percent." in text["markdown"]
    assert "Costs fell 3 percent." in text["markdown"]
    assert [p["method"] for p in text["pages"]] == ["text_layer", "text_layer"]
    assert (text["backend"], text["cached"]) == ("glm_ocr", False)
    assert text["tokens_before"] == 2 * 1600 > text["tokens_after"] > 0
    scan = await _markdown(client, key, scan_id)
    assert [p["method"] for p in scan["pages"]] == ["ocr", "ocr"]
    assert "Recognised text of page 1." in scan["markdown"]
    again = await _markdown(client, key, text_id)
    assert again["cached"] is True
    assert again["markdown"] == text["markdown"]
    assert fake_docparse.PARSES["glm_ocr"] - parsed_before == 2  # each document parsed once


async def test_pages_read_with_low_confidence_keep_the_original(
    test_db: DisposableDatabase,
) -> None:
    async with app_client(app_settings(test_db, ocr_min_confidence=0.95)) as (_app, client):
        key = await _team_key(client, await signup(client))
        scan_id = (await _upload(client, key, "scan.pdf", pdf(1))).json()["id"]
        photo_id = (await _upload(client, key, "photo.png", png(64, 48))).json()["id"]
        scan = await _markdown(client, key, scan_id)
        assert scan["pages"][0]["kept_original"] is True
        assert "[page 1 is attached as an image]" in scan["markdown"]
        assert (await _markdown(client, key, photo_id))["pages"][0]["kept_original"] is True
        parts = [{"type": "text", "text": "Describe:"}, _file(scan_id), _file(photo_id)]
        resp = await _chat(client, key, parts)
        assert resp.status_code == 200, resp.text
        assert int(resp.headers["x-finops-file-tokens-before"]) > 0
        reports = _flags(test_db, resp.headers["x-request-id"])["files"]
        assert [r["kept_pages"] for r in reports] == [[1], [1]]


async def test_switching_the_ocr_backend_needs_only_config_and_artifacts_record_it(
    test_db: DisposableDatabase,
) -> None:
    async with app_client(app_settings(test_db, ocr_backend="glm_ocr")) as (_a, client):
        key = await _team_key(client, await signup(client))
        uploaded = (await _upload(client, key, "scan.pdf", pdf(1))).json()
        assert (await _markdown(client, key, uploaded["id"]))["backend"] == "glm_ocr"
    # A restart with OCR_BACKEND changed: same database, same file, no code change.
    async with app_client(app_settings(test_db, ocr_backend="paddleocr_vl")) as (_b, client):
        switched = await _markdown(client, key, uploaded["id"])
        assert (switched["backend"], switched["cached"]) == ("paddleocr_vl", False)
        assert (await _markdown(client, key, uploaded["id"]))["cached"] is True
    with psycopg.connect(test_db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT backend FROM file_artifacts WHERE sha256 = %s",
            (bytes.fromhex(uploaded["sha256"]),),
        ).fetchall()
    assert {r[0] for r in rows} == {"glm_ocr", "paddleocr_vl"}


async def test_attachments_reach_the_provider_as_markdown_through_the_guardrails(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    key = await _team_key(client, await signup(client))
    note = b"Call jane.doe@example.com about the Q3 plan."
    notes_id = (await _upload(client, key, "notes.txt", note)).json()["id"]
    resp = await _chat(client, key, [{"type": "text", "text": "Summarise:"}, _file(notes_id)])
    assert resp.status_code == 200, resp.text
    reply = resp.json()["choices"][0]["message"]["content"]
    assert '<document name="notes.txt">' in reply  # the mock provider echoes what it received
    assert "[EMAIL_1]" in reply
    assert "jane.doe" not in reply
    assert int(resp.headers["x-finops-file-tokens-after"]) > 0
    files = _flags(test_db, resp.headers["x-request-id"])["files"]
    assert [f["file_id"] for f in files] == [notes_id]


async def test_attachments_obey_file_visibility_and_reference_rules(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    owner, other_team = await _team_key(client, admin), await _team_key(client, admin)
    file_id = (await _upload(client, owner, "plan.txt", b"q3 plan")).json()["id"]
    hidden = await _chat(client, other_team, [_file(file_id)])
    assert (hidden.status_code, hidden.json()["error"]["code"]) == (404, "file_not_found")
    inline = {"type": "file", "file": {"file_data": "data:text/plain;base64,aGk="}}
    for parts, role in (([inline], "user"), ([_file(file_id)], "assistant")):
        resp = await _chat(client, owner, parts, role)
        assert resp.status_code == 422, resp.text
