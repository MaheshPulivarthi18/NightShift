"""Web app routes against the local Postgres, through an in-process ASGI client."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, select

from nightshift.db import Repo, session_scope
from nightshift.web.app import app, render_markdown


def test_render_markdown_sanitizes_and_links_chat() -> None:
    html = render_markdown(
        "<script>alert(1)</script>\n\n[#5](https://github.com/o/r/issues/5) <img src=x onerror=alert(1)>", "o/r"
    )
    assert "<script" not in html and "onerror" not in html
    assert 'href="/chat/o/r/5"' in html


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    try:
        async with session_scope() as s:
            await s.execute(select(1))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_pages_and_adding_a_repo(client: httpx.AsyncClient) -> None:
    name = f"test/web-{uuid.uuid4().hex[:8]}"
    try:
        resp = await client.post("/repos", data={"full_name": f"https://github.com/{name}", "role": "maintainer"})
        assert resp.status_code == 303
        page = await client.get("/repos")
        assert page.status_code == 200 and name in page.text
        assert (await client.get("/jobs")).status_code == 200
        assert (await client.get("/jobs/table")).status_code == 200
        assert (await client.get("/jobs/does-not-exist")).status_code == 404
        assert (await client.get(f"/repos/{name}/report")).status_code == 404  # no report yet
        bad = await client.post("/repos", data={"full_name": "not a repo", "role": "maintainer"})
        assert bad.status_code == 400
    finally:
        async with session_scope() as s:
            await s.execute(delete(Repo).where(Repo.full_name == name))
