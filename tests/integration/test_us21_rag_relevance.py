"""US-21: RAG with relevance filtering. Chunks below the threshold are dropped, tokens pruned are
reported, and a reranker that fails leaves the vector order (fail-open)."""

from typing import Any

import httpx2
import psycopg

from tests.integration.test_us14_files import _team_key
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase

QUESTION = "How many vacation days do employees get each year?"
GOLD = "Vacation policy. Employees get 25 vacation days each year, plus public holidays."
NOISE = (
    "The cafeteria serves lunch from noon until two.",
    "Parking permits are renewed every January.",
    "Printer paper is stored in room 4B.",
)
EVAL = {"dataset": "rag-eval", "metric": "answer_match", "score": 0.97, "threshold": 0.95}


def _sent_to_model(log: list[str]) -> str:
    """The last chat request the gateway sent to the provider."""
    return next(b for b in reversed(log) if '"mock-small"' in b and '"messages"' in b)


def _relevance(db: DisposableDatabase, request_id: str) -> tuple[Any, ...]:
    with psycopg.connect(db.superuser_url) as conn:
        flags = conn.execute(
            "SELECT flags -> 'retrieval' -> 'relevance' FROM usage_events WHERE request_id = %s",
            (request_id,),
        ).fetchone()
        report = conn.execute(
            "SELECT applied, shadow, tokens_before, tokens_after, cost_delta_micro_usd"
            " FROM stage_reports WHERE request_id = %s AND stage = 'rag_relevance'",
            (request_id,),
        ).fetchone()
    assert flags is not None
    return flags[0], report


async def test_us21_passages_under_the_threshold_are_dropped_and_the_pruning_reported(
    test_db: DisposableDatabase,
) -> None:
    log: list[str] = []
    async with app_client(app_settings(test_db), provider_log=log) as (app, client):
        admin = await signup(client)
        auth = bearer(admin["access_token"])
        for i, text in enumerate((GOLD, *NOISE)):
            doc = {"title": f"Doc {i}", "text": text, "access": {"org": True}}
            resp = await client.post("/v1/documents", json=doc, headers=auth)
            assert resp.status_code == 200, resp.text
        key = bearer(await _team_key(client, admin))
        ask = {
            "model": "mock/mock-small",
            "messages": [{"role": "user", "content": QUESTION}],
            "retrieval": {"k": 4},
        }

        async def chat() -> httpx2.Response:
            resp = await client.post("/v1/chat/completions", headers=key, json=ask)
            assert resp.status_code == 200, resp.text
            return resp

        off = await chat()  # the filter ships off: the k nearest go out as before
        assert "cafeteria" in _sent_to_model(log)
        stage = "/v1/admin/stages/rag_relevance"
        assert (await client.put(stage, json={"mode": "shadow"}, headers=auth)).status_code == 200
        shadow = await chat()
        assert "cafeteria" in _sent_to_model(log)  # shadow changes nothing that is sent
        settings = await client.put(
            "/v1/admin/retrieval-settings", json={"threshold": 0.3}, headers=auth
        )
        assert settings.status_code == 200, settings.text
        evals = await client.post(f"{stage}/evals", json={**EVAL, "samples": 50}, headers=auth)
        assert evals.status_code == 201, evals.text
        assert (await client.put(stage, json={"mode": "on"}, headers=auth)).status_code == 200
        on = await chat()
        sent = _sent_to_model(log)
        assert "25 vacation days" in sent
        assert all(noise.split()[1] not in sent for noise in NOISE)  # cafeteria, permits, paper
        assert on.headers["x-finops-retrieved-chunks"] == "1"
        assert int(on.headers["x-finops-pruned-tokens"]) > 0

        class _Down:
            model = "down"

            async def rerank(self, query: str, passages: Any) -> list[float]:
                raise RuntimeError("reranker down")

        app.state.container.retrieval_service._reranker = _Down()
        failed = await chat()  # fail-open: the vector order is kept
        assert "cafeteria" in _sent_to_model(log)

    assert _relevance(test_db, off.headers["x-request-id"]) == (None, None)
    notes, report = _relevance(test_db, shadow.headers["x-request-id"])
    assert (notes["mode"], notes["rerank"], notes["kept"]) == ("shadow", "ok", 1)
    assert report[:2] == (True, True)  # applied, in shadow
    notes, report = _relevance(test_db, on.headers["x-request-id"])
    assert (notes["mode"], notes["kept"], notes["dropped"]) == ("on", 1, 3)
    applied, is_shadow, before, after, delta = report
    assert (applied, is_shadow) == (True, False)
    assert before > after > 0
    assert delta < 0
    notes, report = _relevance(test_db, failed.headers["x-request-id"])
    assert notes["rerank"] == "failed"
    assert report[0] is False
