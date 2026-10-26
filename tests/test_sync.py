"""Background sync: unanswered feedback on your open PRs is kept on the task, and only there.
Uses the local Postgres (docker compose up); skipped when it is not running."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select

from nightshift.db import Repo, Task, session_scope
from nightshift.web.tasks_api import _summary
from nightshift.worker import sync
from nightshift.worker import task as worker
from nightshift.worker.feedback import ReviewComment


@pytest.fixture
async def tasks() -> AsyncIterator[dict[str, int]]:
    name = f"test-sync/{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name)
            s.add(repo)
            await s.flush()
            made: dict[str, int] = {}
            for key, n in (("open", 31), ("merged", 32)):
                t = Task(repo_id=repo.id, issue_number=n, status="published", base_commit="abc",
                         issue={"title": key}, plan={}, result={"pr_url": f"https://github.com/{name}/pull/{n}",
                                                                 "addressed": ["comment:1"]})  # fmt: skip
                s.add(t)
                await s.flush()
                made[key] = t.id
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    made["repo"] = repo.id
    yield made
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_open_prs_keep_their_unanswered_feedback(tasks: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[int] = []

    async def states(repo: str, numbers: list[int], fresh: bool = False) -> dict[int, str]:
        return {31: "OPEN", 32: "MERGED"}

    async def feedback(task_id: int) -> list[ReviewComment]:
        asked.append(task_id)
        return [
            ReviewComment(
                id="thread:5", kind="thread", author="alice", body="@alice: Please rename x.\nThanks", url="u"
            ),
            ReviewComment(id="comment:6", kind="comment", author="ci-bot", body="Coverage 90%", url="u", bot=True),
        ]

    monkeypatch.setattr(sync, "pr_states", states)
    monkeypatch.setattr(worker, "review_feedback", feedback)
    async with session_scope() as s:
        name = (await s.get(Repo, tasks["repo"])).full_name  # type: ignore[union-attr]
    status = await sync.sync_once([name])  # only this test's repo: never the real tasks

    assert asked == [tasks["open"]]  # a merged PR is not asked about
    async with session_scope() as s:
        ids = [tasks["open"], tasks["merged"]]
        rows = {t.id: t for t in (await s.execute(select(Task).where(Task.id.in_(ids)))).scalars()}
    opened = rows[tasks["open"]].result
    assert [c["id"] for c in opened["sync"]["open"]] == ["thread:5"]  # the bot's note is not a request
    assert opened["sync"]["open"][0]["text"] == "@alice: Please rename x."
    assert opened["addressed"] == ["comment:1"]  # the rest of the result is untouched
    assert rows[tasks["merged"]].result["sync"] == {
        **rows[tasks["merged"]].result["sync"],
        "pr_state": "MERGED",
        "open": [],
    }
    assert status["checked"] == 2 and status["error"] is None

    repo = Repo(full_name="o/r")
    assert _summary(rows[tasks["open"]], repo)["feedback"] == 1  # shown in Needs you
    assert _summary(rows[tasks["merged"]], repo)["feedback"] == 0
