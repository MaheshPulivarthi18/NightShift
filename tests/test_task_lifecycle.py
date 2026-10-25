"""Runs that died, archiving, and taking repos off the watchlist."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select

from nightshift.db import Repo, Task, session_scope
from nightshift.sandbox import docker
from nightshift.web.app import app
from nightshift.worker.task import STALE_ERROR, TaskError, archive, mark_stale_runs

JSON = {"content-type": "application/json"}


@pytest.fixture
async def repo() -> AsyncIterator[Repo]:
    name = f"test-life/{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            r = Repo(full_name=name)
            s.add(r)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    yield r
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def _task(repo: Repo, status: str, beat_minutes_ago: float | None) -> int:
    beat = datetime.now(UTC) - timedelta(minutes=beat_minutes_ago) if beat_minutes_ago is not None else None
    async with session_scope() as s:
        t = Task(repo_id=repo.id, issue_number=1, status=status, base_commit="c", issue={}, plan={}, heartbeat_at=beat)
        s.add(t)
        await s.flush()
        return t.id


async def _status(task_id: int) -> tuple[str, str | None]:
    async with session_scope() as s:
        t = await s.get(Task, task_id)
        assert t is not None
        return t.status, t.error


async def test_a_run_that_stopped_beating_needs_you(repo: Repo) -> None:
    dead_run, dead_plan = await _task(repo, "working", 10), await _task(repo, "planning", 10)
    alive = await _task(repo, "working", 0.5)
    stale = await mark_stale_runs()
    assert dead_run in stale and dead_plan in stale and alive not in stale
    assert await _status(dead_run) == ("needs_operator", STALE_ERROR)
    assert await _status(dead_plan) == ("failed", STALE_ERROR)
    assert (await _status(alive))[0] == "working"


async def test_archive_waits_for_a_run_to_stop(repo: Repo) -> None:
    running, done = await _task(repo, "working", 0), await _task(repo, "ready", None)
    with pytest.raises(TaskError, match="once it stops"):
        await archive(running, True)
    await archive(done, True)
    async with session_scope() as s:
        assert (await s.get(Task, done)).archived_at is not None  # type: ignore[union-attr]
    await archive(done, False)
    async with session_scope() as s:
        assert (await s.get(Task, done)).archived_at is None  # type: ignore[union-attr]


async def test_archived_tasks_leave_the_board(repo: Repo) -> None:
    task_id = await _task(repo, "ready", None)
    await archive(task_id, True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        board = {t["id"] for t in (await c.get("/api/tasks")).json()}
        shelf = {t["id"] for t in (await c.get("/api/tasks?archived=true")).json()}
    assert task_id not in board and task_id in shelf


async def test_a_removed_repo_keeps_its_history_and_comes_back(repo: Repo) -> None:
    task_id = await _task(repo, "ready", None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        listed = await c.post(f"/api/repos/{repo.full_name}/remove", json={}, headers=JSON)
        assert repo.full_name not in {r["full_name"] for r in listed.json()}
        async with session_scope() as s:
            assert (await s.get(Task, task_id)) is not None  # the task is still there
        again = await c.post("/api/repos", json={"full_name": repo.full_name}, headers=JSON)
        assert repo.full_name in {r["full_name"] for r in again.json()}
    async with session_scope() as s:
        assert (await s.execute(select(Repo.watched).where(Repo.id == repo.id))).scalar_one() is True


async def test_only_old_task_containers_are_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
    new = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
    removed: list[str] = []

    async def fake_docker(*args: str, **_: Any) -> docker.ExecResult:
        if args[0] == "rm":
            removed.append(args[-1])
            return docker.ExecResult(0, "", "", 0, False)
        return docker.ExecResult(0, f"ns-task-old\t{old}\nns-task-new\t{new}\n", "", 0, False)

    monkeypatch.setattr(docker, "_docker", fake_docker)
    assert await docker.remove_orphans(older_than=3600) == ["ns-task-old"]
    assert removed == ["ns-task-old"]
