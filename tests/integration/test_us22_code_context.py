"""US-22: code questions answered with graph-based context. The context pack holds the right
modules and symbols for the questions, its tokens are far fewer than the whole files', and a
project's context reaches only those who may read it: not another team, not another org."""

from typing import Any
from uuid import uuid4

import httpx2
import orjson
import psycopg
from fastapi import FastAPI

from tests.integration.test_us14_files import _team_key
from tests.integration.test_us21_rag_relevance import _sent_to_model
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase

Api = tuple[FastAPI, httpx2.AsyncClient]
BODY = "\n".join(f"    step_{i} = {i}  # the work, line by line" for i in range(60))
SOURCES = {
    "shop/__init__.py": '"""A small shop."""\n',
    "shop/cart.py": (
        '"""The shopping cart."""\nfrom shop.pricing import price_of\n\n\nclass Cart:\n'
        '    """Items a customer will buy."""\n\n    def total(self) -> int:\n'
        f'        """The cart\'s total price."""\n{BODY.replace("    ", "        ")}\n'
        "        return sum(price_of(i) for i in self.items)\n"
    ),
    "shop/pricing.py": (
        f'"""Prices."""\n\n\ndef price_of(item: str) -> int:\n    """One item\'s price."""\n'
        f"{BODY}\n    return 100\n"
    ),
    "shop/shipping.py": (
        f'"""Shipping."""\n\n\ndef ship(order: str) -> None:\n    """Send an order."""\n{BODY}\n'
    ),
}
QUESTIONS = {  # the eval: each question and the modules its context must hold
    "How does Cart compute its total?": {"shop/cart.py"},
    "What does price_of return for an item?": {"shop/pricing.py"},
    "How are orders sent by ship?": {"shop/shipping.py"},
}


async def _upload(client: httpx2.AsyncClient, auth: dict[str, str], data: Any) -> str:
    body = orjson.dumps(data)
    resp = await client.post(
        "/v1/files",
        headers=auth,
        files={"file": ("bundle.json", body, "application/json")},
        data={"purpose": "user_data"},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])


async def test_us22_context_packs_hold_the_right_modules_at_a_fraction_of_the_tokens(
    api: Api,
) -> None:
    _, client = api
    admin = await signup(client)
    auth = bearer(await _team_key(client, admin))  # a team key creates and indexes the project
    project = await client.post(
        "/v1/projects", json={"name": f"shop-{uuid4().hex[:6]}"}, headers=auth
    )
    assert project.status_code == 200, project.text
    project_id = project.json()["id"]
    file_id = await _upload(client, auth, {"files": SOURCES})
    indexed = await client.post(
        f"/v1/projects/{project_id}/index", json={"file_id": file_id}, headers=auth
    )
    assert indexed.status_code == 200, indexed.text
    assert (indexed.json()["modules"], indexed.json()["source"]) == (4, "python")

    hits = 0
    for question, expected in QUESTIONS.items():
        resp = await client.get(
            f"/v1/projects/{project_id}/context", params={"q": question}, headers=auth
        )
        assert resp.status_code == 200, resp.text
        context = resp.json()
        paths = {p["path"] for p in context["packs"]}
        hits += expected <= paths
        assert context["tokens"] < context["source_tokens"] / 3  # far fewer than whole files
    assert hits == len(QUESTIONS)  # the context-pack hit rate on the eval questions

    cart = await client.get(
        f"/v1/projects/{project_id}/context",
        params={"q": "How does Cart compute its total?"},
        headers=auth,
    )
    assert {"Cart", "Cart.total"} <= set(cart.json()["symbols"])
    assert "def total(self) -> int" in cart.json()["packs"][0]["text"]

    other_team = bearer(await _team_key(client, admin))  # the same org, another team
    hidden = await client.get(
        f"/v1/projects/{project_id}/context", params={"q": "Cart total"}, headers=other_team
    )
    assert hidden.status_code == 404
    shared = await client.post(  # shared with the org: others read it, only its team indexes it
        "/v1/projects",
        json={"name": f"shared-{uuid4().hex[:6]}", "access": {"org": True}},
        headers=auth,
    )
    shared_id = shared.json()["id"]
    read = await client.get(
        f"/v1/projects/{shared_id}/context", params={"q": "Cart"}, headers=other_team
    )
    assert (read.status_code, read.json()["packs"]) == (200, [])
    refused = await client.post(
        f"/v1/projects/{shared_id}/index", json={"file_id": file_id}, headers=other_team
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "index_not_allowed"
    owned = await client.post(
        f"/v1/projects/{shared_id}/index", json={"file_id": file_id}, headers=auth
    )
    assert owned.status_code == 200, owned.text
    stranger = bearer((await signup(client))["access_token"])  # another org
    denied = await client.get(
        f"/v1/projects/{project_id}/context", params={"q": "Cart total"}, headers=stranger
    )
    assert denied.status_code == 404
    listed = (await client.get("/v1/projects", headers=stranger)).json()["data"]
    assert listed == []

    graph = await _upload(
        client,
        auth,
        {
            "nodes": [{"id": "m", "type": "file", "label": "a.py", "source_file": "a.py"}],
            "links": [],
        },
    )
    again = await client.post(
        f"/v1/projects/{project_id}/index", json={"file_id": graph}, headers=auth
    )
    assert again.json()["source"] == "graphify"
    gone = await client.get(
        f"/v1/projects/{project_id}/context", params={"q": "Cart total"}, headers=auth
    )
    assert gone.json()["packs"] == []  # the graph was replaced whole


async def test_us22_chat_with_code_context_sends_outlines_and_reports_the_saving(
    test_db: DisposableDatabase,
) -> None:
    log: list[str] = []
    async with app_client(app_settings(test_db), provider_log=log) as (_, client):
        admin = await signup(client)
        key = bearer(await _team_key(client, admin))
        project = await client.post("/v1/projects", json={"name": "shop"}, headers=key)
        project_id = project.json()["id"]
        file_id = await _upload(client, key, {"files": SOURCES})
        indexed = await client.post(
            f"/v1/projects/{project_id}/index", json={"file_id": file_id}, headers=key
        )
        assert indexed.status_code == 200, indexed.text
        ask = {
            "model": "mock/mock-small",
            "messages": [{"role": "user", "content": "How does Cart compute its total?"}],
            "code_context": {"project_id": project_id},
        }
        resp = await client.post("/v1/chat/completions", headers=key, json=ask)
        assert resp.status_code == 200, resp.text
        sent = _sent_to_model(log)
        assert "<code_context>" in sent
        assert "def total(self) -> int" in sent
        assert "def price_of(item: str) -> int" in sent  # one call away, in another module
        assert "step_0" not in sent  # outlines, never the source
        tokens = int(resp.headers["x-finops-code-context-tokens"])
        hidden = await client.post(
            "/v1/chat/completions",
            headers=bearer(await _team_key(client, admin)),  # another team cannot read it
            json=ask,
        )
        assert hidden.status_code == 404
    with psycopg.connect(test_db.superuser_url) as conn:
        report = conn.execute(
            "SELECT applied, tokens_before, tokens_after, cost_delta_micro_usd, decisions"
            " FROM stage_reports"
            " WHERE request_id = %s AND stage = 'code_context'",
            (resp.headers["x-request-id"],),
        ).fetchone()
        notes = conn.execute(
            "SELECT flags -> 'code_context' FROM usage_events WHERE request_id = %s",
            (resp.headers["x-request-id"],),
        ).fetchone()
    assert report is not None
    applied, before, after, delta, decisions = report
    assert applied
    assert before == after == tokens  # asked for: nothing saved against files never sent
    assert delta == 0
    assert decisions["whole_module_tokens"] > 3 * tokens  # far fewer than the whole module
    assert notes is not None
    assert notes[0]["modules"] == ["shop/cart.py"]


async def test_us22_project_names_are_unique_per_owner_and_reveal_nothing(api: Api) -> None:
    """Review S-L2: a refused create no longer tells anyone in the org the name of a project
    they cannot read."""
    _, client = api
    admin = await signup(client)
    team_a = bearer(await _team_key(client, admin))
    team_b = bearer(await _team_key(client, admin))
    made = await client.post("/v1/projects", json={"name": "shop"}, headers=team_a)
    assert made.status_code == 200, made.text
    again = await client.post("/v1/projects", json={"name": "shop"}, headers=team_a)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "project_exists")
    other = await client.post("/v1/projects", json={"name": "shop"}, headers=team_b)
    assert other.status_code == 200, other.text  # another owner's private name stays private
