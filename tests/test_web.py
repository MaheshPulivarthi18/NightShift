"""Web API routes against the local Postgres, through an in-process ASGI client."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, select

from nightshift.db import PRCheck, Repo, session_scope
from nightshift.web.app import app
from nightshift.web.common import render_markdown


def test_render_markdown_sanitizes_and_links_chat_and_plan() -> None:
    html = render_markdown(
        "<script>alert(1)</script>\n\n[#5](https://github.com/o/r/issues/5) <img src=x onerror=alert(1)>", "o/r"
    )
    assert "<script" not in html and "onerror" not in html
    assert 'href="#/chat/o/r/5"' in html
    assert 'href="#/?repo=o/r&amp;issue=5"' in html


def test_issues_someone_else_took_get_no_plan_link() -> None:
    html = render_markdown(
        "## Good places to start\n\n[#5](https://github.com/o/r/issues/5)\n\n"
        "## Already being worked on\n\n[#6](https://github.com/o/r/issues/6)\n\n"
        "## Duplicates\n\n[#7](https://github.com/o/r/issues/7)\n",
        "o/r",
    )
    assert "issue=5" in html and "issue=7" in html
    assert "issue=6" not in html and 'href="#/chat/o/r/6"' in html  # still chat-able


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


async def test_a_full_watchlist_refuses_a_new_repo(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from nightshift.config import get_settings

    async with session_scope() as s:
        watched = len((await s.execute(select(Repo.id).where(Repo.watched.is_(True)))).all())
    monkeypatch.setattr(get_settings(), "watchlist_max", watched)
    name = f"test/full-{uuid.uuid4().hex[:8]}"
    try:
        resp = await client.post("/api/repos", json={"full_name": name})
        assert resp.status_code == 409 and "watchlist is full" in resp.json()["detail"]
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


async def test_a_check_posts_as_a_review_only_on_confirm(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nightshift.web import api
    from nightshift.worker.prcheck import PullRequest

    name = f"test/review-{uuid.uuid4().hex[:8]}"
    pr = PullRequest(
        repo=name, number=5, url=f"https://github.com/{name}/pull/5", title="t", body="", author="someone",
        state="open", draft=False, base_ref="main", base_sha="b" * 40, head_sha="n" * 40, files=[],
    )  # fmt: skip
    posted: list[tuple[str, ...]] = []

    async def fetch(_: str) -> PullRequest:
        return pr

    async def post(repo: str, number: int, commit: str, body: str, event: str) -> str:
        posted.append((repo, str(number), commit, body, event))
        return f"https://github.com/{name}/pull/5#pullrequestreview-1"

    monkeypatch.setattr(api, "fetch_pull", fetch)
    monkeypatch.setattr(api, "post_review", post)
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name)
            s.add(repo)
            await s.flush()
            report = (
                f"# PR check: {name}#5 t\n\n**Needs work before review.** {pr.url} by @someone\n\n"
                "## Must fix\n\n- Fix X.\n\n_Checked 2026-10-31 09:00 UTC at ooooooo for $0.43._"
            )
            check = PRCheck(repo_id=repo.id, number=5, url=pr.url, head_sha="o" * 40, run_id="r", status="done",
                            markdown=report)  # fmt: skip
            s.add(check)
            await s.flush()
            check_id = check.id

        draft = (await client.post(f"/api/pr-checks/{check_id}/review", json={})).json()
        assert draft["body"].startswith("**Needs work before review.**") and "$0.43" not in draft["body"]
        assert draft["event"] == "COMMENT" and any("new commits" in w for w in draft["warnings"])
        assert not posted

        done = await client.post(
            f"/api/pr-checks/{check_id}/review",
            json={"confirm": True, "body": "Edited.", "event": "REQUEST_CHANGES", "attribution": True},
        )
        assert done.status_code == 200 and done.json()["posted"]
        assert posted == [(name, "5", "o" * 40, "Edited.\n\nGenerated by NightShift", "REQUEST_CHANGES")]
        view = (await client.get(f"/api/pr-checks/{check_id}")).json()
        assert view["review_url"].endswith("pullrequestreview-1")
    finally:
        async with session_scope() as s:
            await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_the_page_is_told_when_the_server_runs_old_code(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nightshift.web import app as web

    monkeypatch.setattr(web, "RUNNING", "a" * 40)
    monkeypatch.setattr(web, "_latest", (0.0, None))
    monkeypatch.setattr(web, "checkout_commit", lambda: "b" * 40)  # pulled since the server started
    assert (await client.get("/api/version")).json() == {"running": "a" * 40, "latest": "b" * 40, "old": True}
    monkeypatch.setattr(web, "_latest", (0.0, None))
    monkeypatch.setattr(web, "checkout_commit", lambda: None)  # not a git checkout: nothing to say
    assert (await client.get("/api/version")).json()["old"] is False
