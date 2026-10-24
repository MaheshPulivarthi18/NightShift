"""Task board API: reads, and the guard that keeps other websites from driving Gate 2."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select

from nightshift.db import session_scope
from nightshift.web.app import app

MISSING = 999_999


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    try:
        async with session_scope() as s:
            await s.execute(select(1))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_list_tasks(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_unknown_task_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"/api/tasks/{MISSING}")).status_code == 404


async def test_a_form_post_cannot_publish(client: httpx.AsyncClient) -> None:
    """A cross-site HTML form can only send form encodings, never JSON without a preflight."""
    resp = await client.post(f"/api/tasks/{MISSING}/publish", data={"confirm": "true"})
    assert resp.status_code == 415


async def test_another_origin_cannot_publish(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        f"/api/tasks/{MISSING}/publish", json={"confirm": True}, headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    resp = await client.post(
        f"/api/tasks/{MISSING}/publish", json={"confirm": True}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert resp.status_code == 403


async def test_same_origin_json_reaches_the_handler(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        f"/api/tasks/{MISSING}/publish", json={"confirm": False}, headers={"Origin": "http://test"}
    )
    assert resp.status_code == 409  # the guard let it through; the task does not exist


async def test_an_issue_that_is_not_free_is_refused_with_the_reason(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nightshift.worker import task as worker

    async def taken(repo: str, number: int, rehearsal: bool = False) -> None:
        raise worker.TaskError(f"#{number} is not free: open PR #713 by someone references it")

    monkeypatch.setattr(worker, "precheck", taken)
    resp = await client.post("/api/tasks", json={"repo": "o/r", "issue": 705}, headers={"Origin": "http://test"})
    assert resp.status_code == 409
    assert "not free" in resp.json()["detail"]


async def test_models_page_lists_every_agent_in_router_order(client: httpx.AsyncClient, models: object) -> None:
    resp = await client.get("/api/models")
    assert resp.status_code == 200
    data = resp.json()
    agents = {a["agent"]: a for a in data["agents"]}
    assert {"coder", "reviewer", "planner", "setup"} <= set(agents)
    coder = agents["coder"]["routes"]
    assert coder[0]["why"] == "first choice" and "calls" in coder[0]["stats"]
