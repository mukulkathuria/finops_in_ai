"""ADR 0011: graph retrieval for a knowledge domain, chosen by the query's intent.

- A `medical` domain's documents are read into a graph of diseases, symptoms and tests.
- A question that comes near the domain's intent and names its symptoms is answered from the
  graph. The graph meets in the disease with the most of them, and the chunk naming the test that
  diagnoses it comes from a second hop: a vector search misses that chunk.
- Other questions, and documents outside the domain, stay with the vector search.
- The graph shows a caller only what its documents may read."""

from typing import Any

import httpx2
from mock_llm.app import app as mock_provider

from gateway.adapters.extraction.openai_compat import OpenAICompatExtractor
from gateway.adapters.postgres.database import Database
from tests.integration.test_us14_files import _team_key
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase
from worker.jobs.graph_extraction import ExtractionReport, GraphExtraction

MEDICAL = {
    "name": "medical",
    "description": "Diseases, the symptoms they show and the tests that diagnose them.",
    "entity_types": ["disease", "symptom", "test", "treatment"],
    "relation_types": ["has_symptom", "diagnosed_by", "treated_with"],
    "examples": [
        "Which disease causes fever, rash and joint pain?",
        "Which blood test confirms the disease?",
        "A patient has a cough and a fever: which disease is it?",
    ],
    "intent_threshold": 0.3,
}
SHARED = (  # every member of the org may read these
    ("Dengue", "Dengue has symptom fever, rash and joint pain."),
    (
        "Measles",
        "Measles has symptom fever, rash and cough. Measles diagnosed by IgM antibody test.",
    ),
    ("Malaria", "Malaria has symptom fever and chills."),
)
PRIVATE = ("Dengue diagnosis", "Dengue diagnosed by NS1 antigen test.")  # its author only
QUESTION = (
    "A patient has fever, rash and joint pain. Which disease is it, and which blood test"
    " confirms it?"
)


async def _extract(worker_db: Database) -> list[ExtractionReport]:
    """What the worker does in the background: every waiting document, into the graph."""
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=mock_provider)) as http:
        extractor = OpenAICompatExtractor(
            "http://mock-llm:8000/v1", http=http, model="mock-small", api_key="dev", timeout_s=10
        )
        job = GraphExtraction(worker_db, extractor)
        reports = []
        while (report := await job.run_once()) is not None:
            reports.append(report)
    return reports


async def test_a_medical_question_is_answered_from_the_graph_and_others_by_vector(
    test_db: DisposableDatabase, worker_db: Database
) -> None:
    settings = app_settings(
        test_db,
        graph_extractor_base_url="http://mock-llm:8000/v1",
        graph_extractor_model="mock-small",
    )
    async with app_client(settings) as (_, client):
        admin = await signup(client)
        auth = bearer(admin["access_token"])
        made = await client.post("/v1/admin/knowledge-domains", json=MEDICAL, headers=auth)
        assert made.status_code == 200, made.text
        domain = made.json()
        assert domain["relation_types"] == MEDICAL["relation_types"]
        twice = await client.post("/v1/admin/knowledge-domains", json=MEDICAL, headers=auth)
        assert twice.status_code == 409

        async def ingest(title: str, text: str, **more: Any) -> dict[str, Any]:
            body = {"title": title, "text": text, **more}
            resp = await client.post("/v1/documents", json=body, headers=auth)
            assert resp.status_code == 200, resp.text
            out: dict[str, Any] = resp.json()
            return out

        for title, text in SHARED:
            filed = await ingest(title, text, access={"org": True}, knowledge_domain="medical")
            assert filed["graph_status"] == "pending"
        await ingest(*PRIVATE, knowledge_domain="medical")
        invoices = await ingest(
            "Supplier invoices",
            "Supplier invoices are paid within 30 days of receipt.",
            access={"org": True},
        )
        assert invoices["graph_status"] == "none"  # not every document goes to the graph
        unknown = await client.post(
            "/v1/documents",
            json={
                "title": "Contracts",
                "text": "Contracts renew yearly.",
                "knowledge_domain": "legal",
            },
            headers=auth,
        )
        assert unknown.status_code == 404

        reports = await _extract(worker_db)
        assert sorted(r.status for r in reports) == ["ready"] * 4
        assert sum(r.relations for r in reports) == 3 + 4 + 2 + 1
        listed = (await client.get("/v1/documents", headers=auth)).json()["data"]
        assert {d["title"]: d["graph_status"] for d in listed} == {
            "Dengue": "ready",
            "Measles": "ready",
            "Malaria": "ready",
            "Dengue diagnosis": "ready",
            "Supplier invoices": "none",
        }

        key = bearer(await _team_key(client, admin))  # may read the org's documents only

        async def search(query: str, headers: dict[str, str], k: int = 3) -> dict[str, Any]:
            body = {"query": query, "k": k}
            resp = await client.post("/v1/retrieval/search", json=body, headers=headers)
            assert resp.status_code == 200, resp.text
            out: dict[str, Any] = resp.json()
            return out

        mine = await search(QUESTION, auth)
        assert mine["route"] == "graph"
        assert mine["knowledge_domain"] == "medical"
        # the graph meets in dengue (all three symptoms), then measles (two); the test that
        # diagnoses dengue is a second hop, in a passage that names no symptom
        assert [h["title"] for h in mine["data"]] == ["Dengue", "Dengue diagnosis", "Measles"]
        assert mine["data"][0]["score"] == 1.0
        team = await search(QUESTION, key)
        assert team["route"] == "graph"
        assert "Dengue diagnosis" not in [h["title"] for h in team["data"]]  # not its to read
        assert [h["title"] for h in team["data"]][:2] == ["Dengue", "Measles"]

        paid = await search("When are supplier invoices paid?", key)
        assert paid["route"] == "vector"  # far from the medical intent
        assert paid["data"][0]["title"] == "Supplier invoices"
        vague = await search("Which disease and which blood test?", key)
        assert vague["route"] == "vector"  # names no entity of the graph
        elsewhere = await search(QUESTION, bearer((await signup(client))["access_token"]))
        assert elsewhere == {
            "object": "list",
            "data": [],
            "route": "vector",
            "knowledge_domain": None,
        }

        ask = {
            "model": "mock/mock-small",
            "messages": [{"role": "user", "content": QUESTION}],
            "retrieval": {"k": 3},
        }
        chat = await client.post("/v1/chat/completions", json=ask, headers=key)
        assert chat.status_code == 200, chat.text
        assert chat.headers["x-finops-retrieval-route"] == "graph"

        domains = (await client.get("/v1/admin/knowledge-domains", headers=auth)).json()["data"]
        assert [d["name"] for d in domains] == ["medical"]
        gone = await client.delete(f"/v1/admin/knowledge-domains/{domain['id']}", headers=auth)
        assert gone.status_code == 200, gone.text
        plain = await search(QUESTION, auth)
        assert plain["route"] == "vector"
        assert "Dengue diagnosis" not in [h["title"] for h in plain["data"]]  # the vector misses it


async def test_without_an_extraction_model_documents_cannot_be_filed_under_a_domain(
    api: tuple[Any, httpx2.AsyncClient],
) -> None:
    _, client = api
    auth = bearer((await signup(client))["access_token"])
    made = await client.post("/v1/admin/knowledge-domains", json=MEDICAL, headers=auth)
    assert made.status_code == 200, made.text
    body = {"title": "Dengue", "text": SHARED[0][1], "knowledge_domain": "medical"}
    resp = await client.post("/v1/documents", json=body, headers=auth)
    assert resp.status_code == 422  # InvalidInput, as every request this gateway cannot serve
    assert resp.json()["error"]["code"] == "graph_extraction_off"
