"""US-14: uploaded files are validated from their bytes, and a refusal names its reason.

Acceptance: a wrong extension with a real MIME mismatch, oversize, over the page limit, zip
bombs and the malware test file are rejected with clear errors; so are the negative catalog's
corrupt and password-protected PDFs and path-traversal names. Accepted files are stored, listed,
downloaded and deleted, and only the uploader's team can see them.
"""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
from fastapi import FastAPI

from gateway.domain.files import _eicar
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase
from tests.unit.test_files import bomb, office, pdf, png

Api = tuple[FastAPI, httpx2.AsyncClient]


def _objects(root: Path, hex_id: str) -> list[Path]:
    """Stored objects named after a file id (dashes removed)."""
    return [p for p in root.rglob("*") if p.is_file() and p.name.replace("-", "") == hex_id]


async def _team_key(client: httpx2.AsyncClient, admin: dict[str, Any]) -> str:
    auth = bearer(admin["access_token"])
    team = (
        await client.post("/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=auth)
    ).json()["id"]
    created = await client.post(
        f"/v1/admin/teams/{team}/api-keys", json={"name": "k"}, headers=auth
    )
    return str(created.json()["key"])


async def _upload(
    client: httpx2.AsyncClient, key: str, name: str, data: bytes, purpose: str = "user_data"
) -> httpx2.Response:
    return await client.post(
        "/v1/files",
        headers=bearer(key),
        files={"file": (name, data, "application/octet-stream")},
        data={"purpose": purpose},
    )


async def test_accepted_files_are_stored_listed_downloaded_and_deleted(api: Api) -> None:
    app, client = api
    admin = await signup(client)
    key = await _team_key(client, admin)
    uploads = {
        "report.pdf": pdf(2),
        "chart.png": png(320, 200),
        "notes.docx": office(),
        "data.csv": b"region,amount\nemea,10\n",
    }
    ids: dict[str, str] = {}
    for name, data in uploads.items():
        resp = await _upload(client, key, name, data)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"].startswith("file-")
        assert (body["object"], body["filename"], body["bytes"]) == ("file", name, len(data))
        ids[name] = body["id"]
    assert (await _upload(client, key, "report.pdf", pdf(2))).json()["pages"] == 2
    listed = (await client.get("/v1/files", headers=bearer(key))).json()
    assert set(ids.values()) <= {f["id"] for f in listed["data"]}
    one = await client.get(f"/v1/files/{ids['chart.png']}", headers=bearer(key))
    assert one.json()["mime_type"] == "image/png"
    content = await client.get(f"/v1/files/{ids['notes.docx']}/content", headers=bearer(key))
    assert content.content == uploads["notes.docx"]
    assert content.headers["content-disposition"].startswith("attachment;")
    assert content.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in content.headers["content-security-policy"]
    root = Path(app.state.container.settings.object_store_path)
    hex_id = ids["notes.docx"].removeprefix("file-")
    # The object key comes from the file id, never from the file's name.
    assert len(await asyncio.to_thread(_objects, root, hex_id)) == 1
    deleted = await client.delete(f"/v1/files/{ids['notes.docx']}", headers=bearer(key))
    assert deleted.json() == {"id": ids["notes.docx"], "object": "file", "deleted": True}
    gone = await client.get(f"/v1/files/{ids['notes.docx']}", headers=bearer(key))
    assert (gone.status_code, gone.json()["error"]["code"]) == (404, "file_not_found")
    assert await asyncio.to_thread(_objects, root, hex_id) == []


async def test_files_are_visible_to_the_uploading_team_only(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    owner_key, other_team_key = await _team_key(client, admin), await _team_key(client, admin)
    other_org_key = await _team_key(client, await signup(client))
    file_id = (await _upload(client, owner_key, "plan.txt", b"q3 plan")).json()["id"]
    for key in (other_team_key, other_org_key):
        assert (await client.get(f"/v1/files/{file_id}", headers=bearer(key))).status_code == 404
        content = await client.get(f"/v1/files/{file_id}/content", headers=bearer(key))
        assert content.status_code == 404
        assert (await client.delete(f"/v1/files/{file_id}", headers=bearer(key))).status_code == 404
        listed = (await client.get("/v1/files", headers=bearer(key))).json()["data"]
        assert file_id not in {f["id"] for f in listed}
    assert (await client.get(f"/v1/files/{file_id}", headers=bearer(owner_key))).status_code == 200


async def test_refusals_name_the_reason_and_store_nothing(test_db: DisposableDatabase) -> None:
    settings = app_settings(test_db, max_upload_bytes=256 * 1024, max_file_pages=3)
    async with app_client(settings) as (_app, client):
        key = await _team_key(client, await signup(client))
        cases = [
            ("photo.pdf", png(10, 10), 422, "file_type_mismatch"),
            ("big.txt", b"a" * (300 * 1024), 413, "file_too_large"),
            ("long.pdf", pdf(5), 422, "too_many_pages"),
            ("bomb.docx", bomb(), 422, "archive_bomb"),
            ("eicar.txt", _eicar(), 422, "malware_detected"),
            ("locked.pdf", pdf(1, password="s3cret"), 422, "encrypted_file"),
            ("broken.pdf", b"%PDF-1.7\n" + b"\x00garbage" * 50, 422, "corrupt_file"),
            ("../../etc/passwd.txt", b"root:x:0:0", 422, "invalid_filename"),
            ("tool.exe", b"MZ" + bytes(64), 422, "unsupported_file_type"),
            ("empty.txt", b"", 422, "empty_file"),
        ]
        for name, data, status, code in cases:
            resp = await _upload(client, key, name, data)
            assert (resp.status_code, resp.json()["error"]["code"]) == (status, code), name
            assert resp.json()["error"]["message"], name
        bad_purpose = await _upload(client, key, "ok.txt", b"fine", purpose="anything")
        assert bad_purpose.json()["error"]["code"] == "invalid_purpose"
        assert (await client.get("/v1/files", headers=bearer(key))).json()["data"] == []
