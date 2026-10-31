"""The track record: outcomes from GitHub, the totals, and adding a pull request made earlier.
Uses the local Postgres (docker compose up) for adding; GitHub is faked."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete

from nightshift.config import get_settings
from nightshift.db import Repo, session_scope
from nightshift.worker import record
from nightshift.worker.record import _facts_of, summary


def _pr(state: str, *reviews: tuple[str, str], merged: str | None = None) -> dict[str, Any]:
    return {"title": "t", "state": state, "createdAt": "2026-10-24T10:00:00Z", "mergedAt": merged, "closedAt": None,
            "author": {"login": "me"},
            "reviews": {"nodes": [{"state": s, "author": {"login": who}} for who, s in reviews]}}  # fmt: skip


def test_the_outcome_follows_github() -> None:
    assert _facts_of(_pr("MERGED", merged="2026-10-25T10:00:00Z"))["outcome"] == "merged"
    assert _facts_of(_pr("CLOSED"))["outcome"] == "closed"
    assert _facts_of(_pr("OPEN"))["outcome"] == "waiting"
    assert _facts_of(_pr("OPEN", ("maint", "APPROVED")))["outcome"] == "approved"
    # headroom #4456: changes requested, then approved by the same reviewer -> approved (each reviewer's latest)
    assert _facts_of(_pr("OPEN", ("jd", "CHANGES_REQUESTED"), ("jd", "APPROVED")))["outcome"] == "approved"
    assert _facts_of(_pr("OPEN", ("a", "APPROVED"), ("b", "CHANGES_REQUESTED")))["outcome"] == "changes requested"
    assert _facts_of(_pr("OPEN", ("me", "APPROVED")))["outcome"] == "waiting"  # the author's own review is not one


def test_the_totals() -> None:
    def row(outcome: str, cost: float, days: float | None = None, imported: bool = False) -> dict[str, Any]:
        return {"outcome": outcome, "cost_usd": cost, "days_to_merge": days, "imported": imported}

    s = summary([row("merged", 0.75, 0.6), row("merged", 0.0, 2.0, imported=True), row("approved", 0.4),
                 row("waiting", 4.55), row("closed", 1.0)])  # fmt: skip
    assert (s["prs"], s["merged"], s["accepted"], s["closed"], s["merge_rate"]) == (5, 2, 3, 1, 0.67)
    assert s["median_days_to_merge"] == 1.3
    assert (s["with_cost"], s["total_cost_usd"], s["median_cost_usd"]) == (4, 6.7, 0.88)  # imported: no cost
    assert s["cost_per_merged_usd"] == 0.75


class FakeGitHub:
    def __init__(self, author: str) -> None:
        self.author = author

    async def rest_get(self, path: str) -> dict[str, Any]:
        number = int(path.rsplit("/", 1)[1])
        return {"html_url": f"https://github.com/{self.repo}/pull/{number}", "title": "Reset readiness events",
                "user": {"login": self.author}, "base": {"sha": "abc", "repo": {"full_name": self.repo}}}  # fmt: skip

    async def aclose(self) -> None:
        pass


@pytest.fixture
async def repo_name(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    name = f"test-record/{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(get_settings(), "github_login", "me")
    try:
        async with session_scope() as s:
            s.add(Repo(full_name=name))
    except OSError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    FakeGitHub.repo = name  # type: ignore[attr-defined]
    yield name
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_an_earlier_pr_of_yours_is_added_once_and_archived(
    repo_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record, "GitHubClient", lambda: FakeGitHub("me"))
    url = f"https://github.com/{repo_name}/pull/169"
    task = await record.add_pr(url)
    assert task.status == "published" and task.archived_at is not None  # counted here, never on a board
    assert task.result == {"pr_url": url, "imported": True}
    with pytest.raises(record.RecordError, match="already in the track record"):
        await record.add_pr(url)
    monkeypatch.setattr(record, "GitHubClient", lambda: FakeGitHub("someone-else"))
    with pytest.raises(record.RecordError, match="not you"):
        await record.add_pr(f"https://github.com/{repo_name}/pull/170")
    with pytest.raises(record.RecordError, match="pull request link"):
        await record.add_pr("not a link")
