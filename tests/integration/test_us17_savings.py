"""US-17: finance sees baseline versus actual cost by team, model and technique, matching the
ledger totals, with conversion and classifier overhead subtracted."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx2
import psycopg
from fastapi import FastAPI

from tests.integration.test_us14_files import _team_key, _upload
from tests.support.app import bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user
from tests.unit.test_files import pdf

MESSY = "Summarise   the   attached   scan,  please:\n\n\n\nkeep  it  short."
EVAL = {"dataset": "d", "metric": "m", "score": 1.0, "threshold": 0.9, "samples": 10}


def _ledger_sums(db: DisposableDatabase, org: str) -> tuple[int, int, int, int]:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT count(*), sum(cost_micro_usd), sum(baseline_micro_usd),"
            " sum(overhead_micro_usd) FROM usage_events WHERE org_id = %s",
            (org,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)


def _window() -> dict[str, str]:
    today = datetime.now(UTC).date()
    return {"start": str(today - timedelta(days=1)), "end": str(today + timedelta(days=2))}


async def test_finance_sees_baseline_versus_actual_by_team_model_and_technique(
    api: tuple[FastAPI, httpx2.AsyncClient], test_db: DisposableDatabase
) -> None:
    _, client = api
    admin = await signup(client)
    auth = bearer(admin["access_token"])
    org = admin["org"]["id"]
    key = await _team_key(client, admin)
    settings = await client.put(
        "/v1/admin/savings-settings", json={"baseline_model": "mock/mock-large"}, headers=auth
    )
    assert settings.json() == {"baseline_model": "mock/mock-large"}
    stages = "/v1/admin/stages/compression"
    await client.post(f"{stages}/evals", json=EVAL, headers=auth)
    assert (await client.put(stages, json={"mode": "on"}, headers=auth)).status_code == 200

    scan = (await _upload(client, key, "scan.pdf", pdf(2))).json()["id"]
    parts: list[dict[str, Any]] = [
        {"type": "text", "text": MESSY},
        {"type": "file", "file": {"file_id": scan}},
    ]
    chat = await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={"model": "mock/mock-small", "messages": [{"role": "user", "content": parts}]},
    )
    assert chat.status_code == 200, chat.text

    count, cost, baseline, overhead = _ledger_sums(test_db, org)
    assert count == 1
    assert baseline > cost  # the original scan, at the baseline model, in full
    assert overhead >= 2 * 0.1 * 300  # two OCR pages at 0.1 GPU second each

    report = await client.get(
        "/v1/reports/savings", params={**_window(), "group_by": "model"}, headers=auth
    )
    assert report.status_code == 200, report.text
    body = report.json()
    totals = body["totals"]
    assert (totals["requests"], totals["cost_micro_usd"]) == (count, cost)
    assert (totals["baseline_micro_usd"], totals["overhead_micro_usd"]) == (baseline, overhead)
    assert totals["savings_micro_usd"] == baseline - cost - overhead
    assert [r["key"] for r in body["rows"]] == ["mock/mock-small"]
    techniques = {t["technique"]: t for t in body["techniques"]}
    assert techniques["compression"]["tokens_saved"] > 0
    assert techniques["file_conversion"]["tokens_saved"] > 2_000  # two pages as Markdown
    assert techniques["file_conversion"]["saved_micro_usd"] > 0

    by_team = await client.get(
        "/v1/reports/savings", params={**_window(), "group_by": "team"}, headers=auth
    )
    (team_row,) = by_team.json()["rows"]
    assert team_row["baseline_micro_usd"] == baseline
    assert team_row["label"]


async def test_reports_are_for_finance_and_settings_for_admins(
    api: tuple[FastAPI, httpx2.AsyncClient], test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    auth = bearer(admin["access_token"])
    org = UUID(admin["org"]["id"])
    member = bearer(app.state.container.tokens.issue_access(add_user(test_db, org), org))
    window = _window()
    assert (
        await client.get("/v1/reports/savings", params=window, headers=member)
    ).status_code == 403
    denied = await client.put(
        "/v1/admin/savings-settings", json={"baseline_model": "mock/mock-large"}, headers=member
    )
    assert denied.status_code == 403
    unknown = await client.put(
        "/v1/admin/savings-settings", json={"baseline_model": "mock/nope"}, headers=auth
    )
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (422, "model_not_found")
    backwards = {"start": window["end"], "end": window["start"]}
    assert (
        await client.get("/v1/reports/savings", params=backwards, headers=auth)
    ).status_code == 422
    empty = (await client.get("/v1/reports/savings", params=window, headers=auth)).json()
    assert (empty["totals"]["requests"], empty["rows"], empty["techniques"]) == (0, [], [])
