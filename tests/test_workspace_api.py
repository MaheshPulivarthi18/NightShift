"""The repo workspace's items: tasks and PR checks, open or closed by their PR's state on GitHub.
Uses the local Postgres (docker compose up); skipped when it is not running."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from nightshift.db import PRCheck, Repo, Task, session_scope
from nightshift.web import workspace_api


@pytest.fixture
async def repo() -> AsyncIterator[Repo]:
    name = f"test-ws/{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            r = Repo(full_name=name)
            s.add(r)
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    async with session_scope() as s:
        row = (await s.execute(select(Repo).where(Repo.full_name == name))).scalar_one()
    yield row
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


def _task(repo_id: int, number: int, status: str, pr: int | None = None, archived: bool = False) -> Task:
    return Task(
        repo_id=repo_id, issue_number=number, status=status, base_commit="abc", issue={"title": f"issue {number}"},
        plan={}, result={"pr_url": f"https://github.com/o/r/pull/{pr}"} if pr else {},
        archived_at=datetime.now(UTC) if archived else None,
    )  # fmt: skip


async def test_items_are_open_or_closed_by_their_prs_state(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as s:
        s.add_all([
            _task(repo.id, 1, "published", pr=11),  # merged on GitHub: closed
            _task(repo.id, 2, "published", pr=12),  # still open: in review
            _task(repo.id, 3, "planned"),  # a plan waiting at Gate 1
            _task(repo.id, 4, "rejected"),  # plan rejected: closed
            _task(repo.id, 5, "ready", archived=True),  # hidden, still listed
            PRCheck(repo_id=repo.id, number=21, url="https://github.com/o/r/pull/21", title="incoming",
                    author="someone", run_id=uuid.uuid4().hex, status="done", verdict="needs work", must=2),
        ])  # fmt: skip
    asked: list[list[int]] = []

    async def fake_states(name: str, numbers: list[int]) -> dict[int, str]:
        asked.append(sorted(numbers))
        return {11: "MERGED", 12: "OPEN", 21: "CLOSED"}

    monkeypatch.setattr(workspace_api, "pr_states", fake_states)
    owner, name = repo.full_name.split("/")
    items = {(i["kind"], i["number"]): i for i in await workspace_api.repo_items(owner, name)}

    assert asked == [[11, 12, 21]]  # one batched question for every PR
    merged = items[("task", 1)]
    assert (merged["open"], merged["pr_state"], merged["role"]) == (False, "MERGED", "yours")
    assert items[("task", 2)]["open"] and items[("task", 2)]["stage"] == "done"
    planned = items[("task", 3)]
    assert planned["open"] and planned["role"] == "issue" and planned["stage"] == "gate1"
    assert not items[("task", 4)]["open"]
    assert items[("task", 5)]["hidden"] and items[("task", 5)]["open"]
    check = items[("check", 21)]
    assert (check["open"], check["role"], check["must"]) == (False, "incoming", 2)
