"""Phase 2 review follow-ups, end to end on PostgreSQL 19 (spec section 12).

Each credential has a request limit and a separate limit on heavy work; a request its budget
cannot cover is refused before its files are converted; an organisation's index has a quota;
pages kept as images stay out of the index; a retrieved passage the policy blocks on is dropped
from the prompt instead of blocking the request; and an exact search backs the vector index when
its scan gives up before finding the k nearest chunks the caller may read.

Key-shaped strings are assembled at runtime so secret scanners never see them in the source.
"""

from typing import Any
from uuid import UUID, uuid7

import psycopg
import pytest
from sqlalchemy import text

from gateway.adapters.postgres import documents as documents_module
from gateway.adapters.postgres.database import TenantContext, fetch_all
from gateway.adapters.postgres.documents import DocumentRepository
from tests.integration.test_us14_files import _team_key, _upload
from tests.support import fake_docparse
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase
from tests.unit.test_files import pdf, text_pdf


def _flags(db: DisposableDatabase, request_id: str) -> dict[str, Any]:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT flags FROM usage_events WHERE request_id = %s", (request_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0])


async def test_each_credential_has_a_request_limit_and_a_separate_heavy_work_limit(
    test_db: DisposableDatabase,
) -> None:
    # A one-minute window may roll over mid-test, so each check sends 2L+1 requests: at least
    # one window then holds more than L of them.
    settings = app_settings(test_db, rate_limit_per_minute=5, heavy_rate_limit_per_minute=1)
    async with app_client(settings) as (_app, client):
        admin = await signup(client)
        heavy, busy, quiet = [await _team_key(client, admin) for _ in range(3)]
        uploads = [await _upload(client, heavy, f"n{i}.txt", b"a note") for i in range(3)]
        refused = [r for r in uploads if r.status_code == 429]
        assert refused, [r.status_code for r in uploads]
        assert all(r.json()["error"]["code"] == "rate_limited" for r in refused)
        assert all(int(r.headers["retry-after"]) >= 1 for r in refused)
        listed = await client.get("/v1/files", headers=bearer(heavy))  # ordinary work goes on
        assert listed.status_code == 200, listed.text
        assert len(listed.json()["data"]) == len(uploads) - len(refused)  # nothing stored
        calls = [await client.get("/v1/whoami", headers=bearer(busy)) for _ in range(11)]
        assert 429 in [r.status_code for r in calls]
        assert (await client.get("/v1/whoami", headers=bearer(quiet))).status_code == 200


async def test_a_request_its_budget_cannot_cover_is_refused_before_its_files_are_converted(
    test_db: DisposableDatabase,
) -> None:
    async with app_client(app_settings(test_db)) as (_app, client):
        admin = await signup(client)
        key = await _team_key(client, admin)
        scan = (await _upload(client, key, "scan.pdf", pdf(1))).json()["id"]
        budget = await client.put(
            "/v1/admin/budgets",
            json={"period": "day", "unit": "tokens", "scope_type": "org", "limit_amount": 1},
            headers=bearer(admin["access_token"]),
        )
        assert budget.status_code in (200, 201), budget.text
        parses = dict(fake_docparse.PARSES)
        parts = [
            {"type": "text", "text": "Summarise:"},
            {"type": "file", "file": {"file_id": scan}},
        ]
        resp = await client.post(
            "/v1/chat/completions",
            headers=bearer(key),
            json={
                "model": "mock/mock-small",
                "messages": [{"role": "user", "content": parts}],
                "max_tokens": 50,
                "retrieval": {"k": 3},
            },
        )
    assert (resp.status_code, resp.json()["error"]["code"]) == (429, "budget_exceeded")
    assert dict(fake_docparse.PARSES) == parses  # the scan was never converted


async def test_an_index_quota_keeps_an_organisation_from_filling_the_database(
    test_db: DisposableDatabase,
) -> None:
    paragraph = "word " * 200  # about 1,000 characters: one chunk each
    body = {"text": f"{paragraph}\n\n{paragraph}"}
    async with app_client(app_settings(test_db, max_indexed_chunks_per_org=3)) as (_app, client):
        admin = bearer((await signup(client))["access_token"])
        first = await client.post("/v1/documents", json={"title": "A", **body}, headers=admin)
        second = await client.post("/v1/documents", json={"title": "B", **body}, headers=admin)
    assert first.status_code == 200, first.text
    assert first.json()["chunks"] == 2
    assert (second.status_code, second.json()["error"]["code"]) == (403, "index_quota_exceeded")


async def test_pages_read_with_low_confidence_stay_out_of_the_index(
    test_db: DisposableDatabase,
) -> None:
    async with app_client(app_settings(test_db, ocr_min_confidence=0.95)) as (_app, client):
        key = await _team_key(client, await signup(client))
        mixed = text_pdf("Revenue grew 12 percent.", "")  # page 2 is a scan, read at 0.9
        mixed_id = (await _upload(client, key, "mixed.pdf", mixed)).json()["id"]
        scan_id = (await _upload(client, key, "scan.pdf", pdf(1))).json()["id"]
        made = await client.post(
            "/v1/documents", json={"title": "Mixed", "file_id": mixed_id}, headers=bearer(key)
        )
        empty = await client.post(
            "/v1/documents", json={"title": "Scan", "file_id": scan_id}, headers=bearer(key)
        )
    assert made.status_code == 200, made.text
    assert made.json()["warnings"] == ["pages read with low confidence are not indexed: 2"]
    assert (empty.status_code, empty.json()["error"]["code"]) == (422, "empty_document")
    with psycopg.connect(test_db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT content FROM chunks WHERE document_id = %s", (made.json()["id"],)
        ).fetchall()
    assert [r[0] for r in rows] == ["Revenue grew 12 percent."]


async def test_a_retrieved_passage_the_policy_blocks_on_is_dropped_not_the_request(
    test_db: DisposableDatabase,
) -> None:
    secret = "AK" + "IA" + "QWERTYUIOPASDFGH"
    sent: list[str] = []  # every body the gateway sends the mock servers
    async with app_client(app_settings(test_db), provider_log=sent) as (_app, client):
        key = bearer(await _team_key(client, await signup(client)))
        for title, content in (("Runbook", "Deploy with the blue pipeline."), ("Old", secret)):
            made = await client.post(
                "/v1/documents", json={"title": title, "text": f"deploy: {content}"}, headers=key
            )
            assert made.status_code == 200, made.text
        sent.clear()  # the ingestion's embedding calls; what follows is the chat request's
        resp = await client.post(
            "/v1/chat/completions",
            headers=key,
            json={
                "model": "mock/mock-small",
                "messages": [{"role": "user", "content": "How do we deploy?"}],
                "max_tokens": 200,
                "retrieval": {"k": 5},
            },
        )
    assert resp.status_code == 200, resp.text  # before: blocked, as if the user sent the key
    assert any("blue pipeline" in body and "mock-small" in body for body in sent)  # the model
    assert not any("AKIA" in body for body in sent)  # nothing upstream saw the dropped passage
    assert resp.headers["x-finops-retrieved-chunks"] == "1"
    dropped = _flags(test_db, resp.headers["x-request-id"])["retrieval"]["dropped"]
    assert [d["rule"] for d in dropped] == ["aws_access_key"]


_DIMENSIONS = 1024
_QUERY = [1.0] + [0.0] * (_DIMENSIONS - 1)
_INSERT_CHUNK = (
    "INSERT INTO chunks (id, org_id, document_id, ordinal, content, token_count, acl_groups,"
    " embedding_model, embedding) VALUES (%s, %s, %s, %s, %s, 1, %s, 'bge-m3', %s::vector)"
)


def _vector(*weights: tuple[int, float]) -> str:
    values = [0.0] * _DIMENSIONS
    for axis, weight in weights:
        values[axis] = weight
    return "[" + ",".join(f"{v:g}" for v in values) + "]"


def _seed(db: DisposableDatabase, org: UUID) -> set[UUID]:
    """40 chunks right next to the query that only a stranger may read, and 3 far away that the
    whole org may; returns the ids of the 3."""
    stranger, everyone = [f"user:{uuid7()}"], ["org"]
    hidden_doc, visible_doc = uuid7(), uuid7()
    visible = {uuid7() for _ in range(3)}
    with psycopg.connect(db.superuser_url) as conn:
        for doc, groups, count in ((hidden_doc, stranger, 40), (visible_doc, everyone, 3)):
            conn.execute(
                "INSERT INTO documents (id, org_id, title, acl_groups, embedding_model,"
                " chunk_count, token_count) VALUES (%s, %s, 'seeded', %s, 'bge-m3', %s, %s)",
                (doc, org, groups, count, count),
            )
        for i in range(40):
            near = _vector((0, 1.0), (100 + i, 0.01))
            conn.execute(_INSERT_CHUNK, (uuid7(), org, hidden_doc, i, f"h{i}", stranger, near))
        for i, chunk in enumerate(sorted(visible)):
            far = _vector((500 + i, 1.0))
            conn.execute(_INSERT_CHUNK, (chunk, org, visible_doc, i, f"v{i}", everyone, far))
    return visible


async def test_the_exact_search_finds_what_the_index_scan_gives_up_on(
    test_db: DisposableDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An index scan that keeps 10 candidates and does not iterate past them, with the planner
    # pushed onto the index: the 10 nearest chunks are all unreadable to the caller.
    tune = (
        "SET LOCAL hnsw.iterative_scan = relaxed_order",
        "SET LOCAL hnsw.ef_search = 10",
        "SET LOCAL hnsw.max_scan_tuples = 1",
        "SET LOCAL enable_seqscan = off",
        "SET LOCAL enable_bitmapscan = off",
        "SET LOCAL enable_sort = off",
    )
    monkeypatch.setattr(documents_module, "_TUNE", tune)
    async with app_client(app_settings(test_db)) as (app, client):
        org = UUID((await signup(client))["org"]["id"])
        visible = _seed(test_db, org)
        db, context = app.state.container.db, TenantContext(org, None)
        params = {"query": documents_module._vector(_QUERY), "groups": ["org"], "model": "bge-m3"}
        async with db.transaction(context) as conn:
            for statement in tune:
                await conn.execute(text(statement))
            index_only = await fetch_all(conn, documents_module._SEARCH, {**params, "k": 3})
        async with db.transaction(context) as conn:
            hits = await DocumentRepository().search(
                conn, _QUERY, groups=frozenset({"org"}), model="bge-m3", k=3
            )
    assert len(index_only) < 3  # the index scan alone comes back short
    assert {h.chunk_id for h in hits} == visible
