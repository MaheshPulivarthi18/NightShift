"""Your corrections to what NightShift learned about a repo reach every agent: an edited brief replaces the
generated one, your notes are added, and a forgotten piece is learned again. Uses the test database."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, select

from nightshift.db import Repo, RepoContext, session_scope
from nightshift.web.app import app
from nightshift.worker.onboard import BRIEF, your_brief


@pytest.fixture
async def repo() -> AsyncIterator[tuple[int, str]]:
    name = f"test-knowledge/{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            r = Repo(full_name=name)
            s.add(r)
            await s.flush()
            s.add(RepoContext(repo_id=r.id, kind=BRIEF, commit="abc", data={"text": "Generated: use pytest."}))
            repo_id = r.id
    except OSError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    yield repo_id, name
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_your_brief_and_notes_reach_the_agents(repo: tuple[int, str]) -> None:
    repo_id, name = repo
    assert await your_brief(repo_id, "Generated: use pytest.") == "Generated: use pytest."
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        k = (await client.post(f"/api/repos/{name}/knowledge/brief", json={"text": "One PR per issue."})).json()
        assert k["brief"] == "One PR per issue." and k["brief_edited"]
        k = (await client.post(f"/api/repos/{name}/knowledge/notes", json={"text": "Run with -p no:randomly"})).json()
        assert k["notes"] == "Run with -p no:randomly"
        rules = await your_brief(repo_id, "Generated: use pytest.")
        assert rules.startswith("One PR per issue.") and rules.endswith("Run with -p no:randomly")

        assert (await client.post(f"/api/repos/{name}/knowledge/forget", json={"piece": "brief"})).status_code == 200
        assert await your_brief(repo_id, "Generated again.") == (
            "Generated again.\n\nThe developer's own notes about this repo:\nRun with -p no:randomly"
        )  # back to the generated brief; the notes stay
        await client.post(f"/api/repos/{name}/knowledge/notes", json={"text": "  "})  # empty removes them
        assert await your_brief(repo_id, "Generated again.") == "Generated again."
        bad = await client.post(f"/api/repos/{name}/knowledge/forget", json={"piece": "everything"})
        assert bad.status_code == 400
        assert (await client.post(f"/api/repos/{name}/knowledge/brief", json={"text": " "})).status_code == 400
    async with session_scope() as s:
        left = (await s.execute(select(RepoContext).where(RepoContext.repo_id == repo_id))).scalars().all()
    assert left == []
