"""A repo GitHub moved: the watched row takes the new name, merging a row already made for it."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from nightshift.db import Item, Repo, RepoContext, Run, RunEvent, Task, session_scope
from nightshift.memory import follow_rename, purge_repo

NOW = datetime(2026, 10, 25, tzinfo=UTC)


def _item(repo_id: int, number: int, title: str) -> Item:
    return Item(
        repo_id=repo_id, number=number, kind="issue", title=title, body="", author="a", state="OPEN",
        url="u", labels=[], data={}, content_hash=title, created_at=NOW, updated_at=NOW,
    )  # fmt: skip


@pytest.fixture
async def names() -> AsyncIterator[tuple[str, str]]:
    tag = uuid.uuid4().hex[:8]
    old, new = f"test-old/{tag}", f"test-new/{tag}"
    try:
        async with session_scope() as s:
            await s.execute(select(1))
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    yield old, new
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name.in_([old, new])))


async def test_a_watched_repo_takes_its_new_name(names: tuple[str, str]) -> None:
    old, new = names
    async with session_scope() as s:
        s.add(Repo(full_name=old))
    async with session_scope() as s:
        repo = await follow_rename(s, old, new)
    assert repo is not None and repo.full_name == new
    async with session_scope() as s:
        assert (await s.execute(select(Repo).where(Repo.full_name == old))).scalar_one_or_none() is None


async def test_a_row_made_under_the_new_name_is_merged_into_the_old_one(names: tuple[str, str]) -> None:
    old, new = names
    async with session_scope() as s:
        a, b = Repo(full_name=old, role="maintainer"), Repo(full_name=new)
        s.add_all([a, b])
        await s.flush()
        s.add_all([_item(a.id, 1, "old copy"), _item(a.id, 2, "only old"), _item(b.id, 1, "new copy")])
        s.add(RepoContext(repo_id=b.id, kind="environment", commit="c1", data={"status": "ready"}))
        s.add(Task(repo_id=b.id, issue_number=1, status="planned", base_commit="c1", issue={}, plan={}))
        keep_id = a.id
    async with session_scope() as s:
        repo = await follow_rename(s, old, new)
    assert repo is not None and repo.id == keep_id and repo.full_name == new and repo.role == "maintainer"
    async with session_scope() as s:
        items = {i.number: i.title for i in (await s.execute(select(Item).where(Item.repo_id == keep_id))).scalars()}
        assert items == {1: "new copy", 2: "only old"}  # the fresher copy wins a clash
        assert (await s.execute(select(Task).where(Task.repo_id == keep_id))).scalars().first() is not None
        ctx = (await s.execute(select(RepoContext).where(RepoContext.repo_id == keep_id))).scalar_one()
        assert ctx.kind == "environment"
        assert len(list((await s.execute(select(Repo).where(Repo.full_name.in_([old, new])))).scalars())) == 1


async def test_deleting_a_repo_deletes_its_records(names: tuple[str, str]) -> None:
    old, _ = names
    async with session_scope() as s:
        repo = Repo(full_name=old)
        s.add(repo)
        await s.flush()
        s.add(_item(repo.id, 1, "an issue"))
        run_id = f"setup-test-{uuid.uuid4().hex[:8]}"
        s.add(Run(run_id=run_id, repo_id=repo.id, status="done"))
        s.add(RunEvent(run_id=run_id, kind="node_start", node="setup", message="setup", data={}))
        repo_id = repo.id
    async with session_scope() as s:
        stored = await s.get(Repo, repo_id)
        assert stored is not None
        await purge_repo(s, stored)  # as `nightshift watch remove --purge` does
    async with session_scope() as s:
        assert (await s.execute(select(Item).where(Item.repo_id == repo_id))).first() is None
        assert (await s.execute(select(RunEvent).where(RunEvent.run_id == run_id))).first() is None  # its log too
