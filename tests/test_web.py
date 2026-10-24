"""Web API routes against the local Postgres, through an in-process ASGI client."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, select

from nightshift.db import Repo, session_scope
from nightshift.web.app import app
from nightshift.web.common import render_markdown


def test_render_markdown_sanitizes_and_links_chat_and_plan() -> None:
    html = render_markdown(
        "<script>alert(1)</script>\n\n[#5](https://github.com/o/r/issues/5) <img src=x onerror=alert(1)>", "o/r"
    )
    assert "<script" not in html and "onerror" not in html
    assert 'href="#/chat/o/r/5"' in html
    assert 'href="#/?repo=o/r&amp;issue=5"' in html


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    try:
        async with session_scope() as s:
            await s.execute(select(1))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_repos_jobs_and_overview(client: httpx.AsyncClient) -> None:
    name = f"test/web-{uuid.uuid4().hex[:8]}"
    try:
        resp = await client.post("/api/repos", json={"full_name": f"https://github.com/{name}", "role": "maintainer"})
        assert resp.status_code == 200
        assert any(r["full_name"] == name and r["role"] == "maintainer" for r in resp.json())
        owner, repo = name.split("/")
        resp = await client.post(f"/api/repos/{owner}/{repo}/role", json={"role": "contributor"})
        assert any(r["full_name"] == name and r["role"] == "contributor" for r in resp.json())
        assert (await client.get(f"/api/repos/{owner}/{repo}/report")).status_code == 404  # no report yet
        assert (await client.post("/api/repos", json={"full_name": "not a repo"})).status_code == 400
        assert (await client.get("/api/jobs")).status_code == 200
        assert (await client.get("/api/jobs/does-not-exist")).status_code == 404
        overview = (await client.get("/api/overview")).json()
        assert set(overview) == {"spend", "killed"}
    finally:
        async with session_scope() as s:
            await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_writes_need_json(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/control/kill", data={"killed": "true"})
    assert resp.status_code == 415
    resp = await client.post("/api/repos", json={"full_name": "o/r"}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


async def test_index_explains_how_to_build_when_missing(client: httpx.AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code in (200, 503)
