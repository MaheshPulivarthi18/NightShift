"""A plan that asked the maintainers first: replies are watched, and it can be planned again with an answer.
Uses the local Postgres (docker compose up); skipped when it is not running."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select

from nightshift.config import get_settings
from nightshift.db import Repo, Task, session_scope
from nightshift.web import tasks_api
from nightshift.web.app import app
from nightshift.worker import sync
from nightshift.worker import task as worker
from nightshift.worker.issue import IssueView


@pytest.fixture
async def asking() -> AsyncIterator[dict[str, Any]]:
    name = f"test-replan/{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name)
            s.add(repo)
            await s.flush()
            t = Task(repo_id=repo.id, issue_number=50, status="planned", base_commit="abc", issue={"title": "Upgrade"},
                     plan={"actionable": False, "questions": ["Has litellm#38663 been released?"], "planning_note": ""},
                     result={"claim_url": f"https://github.com/{name}/issues/50#issuecomment-1"})  # fmt: skip
            s.add(t)
            await s.flush()
            ids = {"repo": name, "task": t.id}
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    yield ids
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


def _issue(me: str, *comments: tuple[str, str, str]) -> IssueView:
    return IssueView(number=50, title="Upgrade", body="", state="open", author="owner", labels=[], assignees=[],
                     comments=[{"author": a, "created_at": t, "body": b} for a, t, b in comments], linked_prs=[],
                     url="https://github.com/o/r/issues/50")  # fmt: skip


async def test_replies_after_the_question_are_watched(asking: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    me = get_settings().github_login or "me"
    monkeypatch.setattr(get_settings(), "github_login", me)

    async def fetch(repo: str, number: int) -> IssueView:
        return _issue(me, ("owner", "2026-10-26T09:00:00Z", "Before the question."),
                      (me, "2026-10-27T10:00:00Z", "Has it been released?"),
                      ("owner", "2026-10-27T12:00:00Z", "Yes, 1.104.2 is out.\nGo ahead."),
                      ("github-actions[bot]", "2026-10-27T12:01:00Z", "Labelled."))  # fmt: skip

    monkeypatch.setattr("nightshift.worker.issue.fetch_issue", fetch)
    await sync.sync_once([asking["repo"]])
    async with session_scope() as s:
        t = (await s.execute(select(Task).where(Task.id == asking["task"]))).scalar_one()
        repo = (await s.execute(select(Repo).where(Repo.full_name == asking["repo"]))).scalar_one()
    assert [r["text"] for r in t.result["sync"]["open"]] == ["Yes, 1.104.2 is out."]  # not before, not the bot
    summary = tasks_api._summary(t, repo)
    assert (summary["asking"], summary["asked"], summary["feedback"]) == (True, True, 1)


async def test_planning_again_carries_the_answer_and_hides_the_old_plan(
    asking: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    planned: list[tuple[str, int, str]] = []

    async def precheck(repo: str, number: int) -> tuple[str, Any, list[str], None]:
        return repo, None, [], None

    async def plan_task(repo: str, number: int, *, note: str = "", base: str | None = None) -> None:
        planned.append((repo, number, note))

    monkeypatch.setattr(worker, "precheck", precheck)
    monkeypatch.setattr(worker, "plan_task", plan_task)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(f"/api/tasks/{asking['task']}/replan", json={"answer": "Released in 1.104.2: upgrade now"},
                            headers={"Origin": "http://test"})  # fmt: skip
    assert resp.status_code == 202
    for _ in range(50):  # the plan starts in the background
        if planned:
            break
        import asyncio

        await asyncio.sleep(0.02)
    assert planned and planned[0][:2] == (asking["repo"], 50)
    note = planned[0][2]
    assert "Has litellm#38663 been released?" in note and "Released in 1.104.2: upgrade now" in note
    async with session_scope() as s:
        old = await s.get(Task, asking["task"])
    assert old is not None and old.archived_at is not None
