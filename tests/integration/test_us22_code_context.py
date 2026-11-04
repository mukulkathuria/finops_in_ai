"""US-22: code questions answered with graph-based context. The context pack holds the right
modules and symbols for the questions, its tokens are far fewer than the whole files', and a
project's context reaches only those who may read it: not another team, not another org."""

from typing import Any
from uuid import uuid4

import httpx2
import orjson
from fastapi import FastAPI

from tests.support.app import bearer, signup

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


async def _team_key(client: httpx2.AsyncClient, admin: dict[str, Any]) -> str:
    auth = bearer(admin["access_token"])
    team = (
        await client.post("/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=auth)
    ).json()["id"]
    created = await client.post(
        f"/v1/admin/teams/{team}/api-keys", json={"name": "k"}, headers=auth
    )
    return str(created.json()["key"])


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
