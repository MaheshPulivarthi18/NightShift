"""Incoming PRs on repos that opted in are checked on the sync, and wait in Needs you until posted or dismissed.
Uses the local Postgres (docker compose up); GitHub and the checker are faked."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import delete

from nightshift.config import get_settings
from nightshift.db import PRCheck, Repo, session_scope
from nightshift.web.app import app
from nightshift.worker import autocheck, prcheck, sync
from nightshift.worker.autocheck import wanted


def _pr(n: int, login: str = "alice", sha: str = "", **kw: Any) -> dict[str, Any]:
    return {"number": n, "html_url": f"https://github.com/o/r/pull/{n}", "user": {"login": login, "type": "User"},
            "head": {"sha": sha or f"s{n}"}, "draft": False, **kw}  # fmt: skip


def test_only_new_commits_by_others_are_checked() -> None:
    pulls = [_pr(1, sha="a"), _pr(2, draft=True), _pr(3, "dependabot[bot]"), _pr(4, "Me"), _pr(5),
             {**_pr(6), "user": {"login": "renovate", "type": "Bot"}}, _pr(1, sha="b")]  # fmt: skip
    got = wanted(pulls, {(1, "a")}, "me")
    assert [(p["number"], p["head"]["sha"]) for p in got] == [(5, "s5"), (1, "b")]  # a new push is checked again


@pytest.fixture
async def repo_name(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    name = f"test-autocheck/{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(get_settings(), "github_login", "me")
    try:
        async with session_scope() as s:
            s.add(Repo(full_name=name, role="maintainer", auto_check=True))
    except OSError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    yield name
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_the_sync_checks_a_few_new_prs_per_repo(repo_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as s:
        repo = (await s.execute(Repo.__table__.select().where(Repo.full_name == repo_name))).first()
        assert repo is not None
        s.add(PRCheck(repo_id=repo.id, number=1, url="u", head_sha="a", run_id="r", status="done"))
    checked: list[str] = []

    async def pulls(full_name: str) -> list[dict[str, Any]]:
        if full_name != repo_name:
            return []  # the real repos on this machine are left alone
        return [{**_pr(n), "html_url": f"https://github.com/{repo_name}/pull/{n}"} for n in (9, 8, 7, 6)] + [
            {**_pr(1, sha="a"), "html_url": f"https://github.com/{repo_name}/pull/1"}
        ]

    async def check(url: str, run_id: str | None = None, *, auto: bool = False) -> None:
        assert auto
        checked.append(url.rsplit("/", 1)[1])

    monkeypatch.setattr(autocheck, "open_pulls", pulls)
    monkeypatch.setattr(prcheck, "check_pr", check)
    monkeypatch.setattr(get_settings(), "auto_check_per_sync", 3)
    status = await autocheck.autocheck_once()
    assert checked == ["9", "8", "7"] and status["error"] is None  # newest first, three per sync


async def test_finished_checks_wait_until_posted_or_dismissed(repo_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def states(repo: str, numbers: list[int], fresh: bool = False) -> dict[int, str]:
        return {n: "CLOSED" if n == 4 else "OPEN" for n in numbers}

    monkeypatch.setattr(sync, "pr_states", states)
    async with session_scope() as s:
        repo = (await s.execute(Repo.__table__.select().where(Repo.full_name == repo_name))).first()
        assert repo is not None
        rows = [
            PRCheck(repo_id=repo.id, number=1, url="u1", run_id="a", status="done", auto=True, head_sha="old"),
            PRCheck(repo_id=repo.id, number=2, url="u2", run_id="b", status="running", auto=True),
            PRCheck(repo_id=repo.id, number=3, url="u3", run_id="c", status="done", auto=True, review_url="posted"),
            PRCheck(repo_id=repo.id, number=4, url="u4", run_id="d", status="done", auto=True),  # PR closed
            PRCheck(repo_id=repo.id, number=5, url="u5", run_id="e", status="done"),  # checked by hand
        ]
        s.add_all(rows)
        await s.flush()
        s.add(PRCheck(repo_id=repo.id, number=1, url="u1", run_id="f", status="done", auto=True, head_sha="new"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        mine = [c for c in (await client.get("/api/incoming-checks")).json() if c["repo"] == repo_name]
        assert [(c["number"], c["run_id"]) for c in mine] == [(1, "f")]  # the latest check of each PR only
        assert (await client.post(f"/api/pr-checks/{mine[0]['id']}/dismiss", json={})).json() == {"dismissed": True}
        assert not [c for c in (await client.get("/api/incoming-checks")).json() if c["repo"] == repo_name]
        owner, name = repo_name.split("/")
        repos = (await client.post(f"/api/repos/{owner}/{name}/auto-check", json={"on": False})).json()
        assert next(r for r in repos if r["full_name"] == repo_name)["auto_check"] is False
