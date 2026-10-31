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

    async def ci(repo: str, number: int) -> dict[str, object]:
        return {"state": "failed", "failed": ["test (3.12)"], "sha": "abc1234"}

    async def still_open(repo: str, number: int) -> bool:
        return True

    monkeypatch.setattr(sync, "pr_states", states)
    monkeypatch.setattr(sync, "pr_ci", ci)
    monkeypatch.setattr(sync, "issue_open", still_open)
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
        "ci": None,  # CI matters only while the PR is open
        "issue_open": True,  # merged, yet its issue is open
    }
    assert status["checked"] == 2 and status["error"] is None

    repo = Repo(full_name="o/r")
    assert _summary(rows[tasks["open"]], repo)["feedback"] == 1  # shown in Needs you
    assert _summary(rows[tasks["merged"]], repo)["feedback"] == 0
    assert _summary(rows[tasks["open"]], repo)["ci_failing"] == ["test (3.12)"]  # a red PR waits on you
    assert _summary(rows[tasks["merged"]], repo)["ci_failing"] == []


async def test_ci_on_an_open_pr_leaves_advice_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def status(gh: object, repo: str, sha: str) -> dict[str, object]:
        return {"state": "failed", "failed": ["codspeed", "lint"], "pending": [], "passed": 4}

    monkeypatch.setattr(sync, "ci_status", status)
    assert await sync.pr_ci("o/r", 7) is None  # its latest commit is not known yet
    sync._pr_heads[("o/r", 7)] = "abcdef1234"
    assert await sync.pr_ci("o/r", 7) == {"state": "failed", "failed": ["lint"], "sha": "abcdef1"}
    monkeypatch.setattr(sync, "ci_status", lambda *a: _done({"state": "failed", "failed": ["codspeed"],
                                                             "pending": [], "passed": 4}))  # fmt: skip
    assert (await sync.pr_ci("o/r", 7))["state"] == "passed"  # type: ignore[index]  # a benchmark is advice


async def _done(value: dict[str, object]) -> dict[str, object]:
    return value


async def test_a_merged_fix_with_its_issue_open_offers_a_comment(
    tasks: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nightshift.worker import publish

    posted: list[tuple[int, str]] = []

    async def post(repo: Repo, number: int, text: str) -> str:
        posted.append((number, text))
        return "https://github.com/o/r/issues/32#issuecomment-1"

    monkeypatch.setattr(publish, "post_comment", post)
    async with session_scope() as s:
        t = await s.get(Task, tasks["merged"])
        assert t is not None
        t.result = {**t.result, "sync": {"pr_state": "MERGED", "open": [], "issue_open": True}}
    async with session_scope() as s:
        merged = await s.get(Task, tasks["merged"])
    assert _summary(merged, Repo(full_name="o/r"))["fixed_open"]  # type: ignore[arg-type]
    draft = await worker.fixed_comment(tasks["merged"], confirm=False)
    assert "This is fixed by #32, which is merged. Could this issue be closed?" in draft and not posted
    url = await worker.fixed_comment(tasks["merged"], confirm=True, text="Fixed by #32, thanks!")
    assert url.endswith("issuecomment-1") and posted == [(32, "Fixed by #32, thanks!")]
    with pytest.raises(worker.TaskError, match="Already done"):
        await worker.fixed_comment(tasks["merged"], confirm=True)
    async with session_scope() as s:
        merged = await s.get(Task, tasks["merged"])
    assert not _summary(merged, Repo(full_name="o/r"))["fixed_open"]  # type: ignore[arg-type]
    assert await worker.fixed_comment(tasks["open"], confirm=False, dismiss=True) == "dismissed"


async def test_failing_ci_becomes_a_revision_and_the_branch_is_updated_on_click(
    tasks: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nightshift.worker import publish

    class GH:
        async def rest_get(self, path: str) -> dict[str, object]:
            return {"head": {"sha": "abcdef1234"}}

        async def aclose(self) -> None:
            pass

    async def failures(gh: object, repo: str, sha: str) -> list[dict[str, str]]:
        return [{"name": "test (3.12)", "url": "u", "excerpt": "FAILED tests/test_a.py::test_x"}]

    revised: list[tuple[int, str]] = []

    async def revise(task_id: int, note: str) -> Task:
        revised.append((task_id, note))
        return Task()

    updated: list[int] = []

    async def update(repo: Repo, number: int) -> None:
        updated.append(number)

    monkeypatch.setattr(worker, "GitHubClient", GH)
    monkeypatch.setattr(worker, "ci_failures", failures)
    monkeypatch.setattr(worker, "revise", revise)
    monkeypatch.setattr(publish, "update_branch", update)
    await worker.fix_ci(tasks["open"])
    ((task_id, note),) = revised
    assert task_id == tasks["open"] and "CI fails on the pull request at abcdef1" in note
    assert "<ci_output>\n### test (3.12)\n```\nFAILED tests/test_a.py::test_x\n```\n</ci_output>" in note

    async def none(gh: object, repo: str, sha: str) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(worker, "ci_failures", none)
    with pytest.raises(worker.TaskError, match="No CI job fails"):
        await worker.fix_ci(tasks["open"])

    assert "Would merge the base branch" in await worker.update_branch(tasks["open"], confirm=False) and not updated
    assert "Merged the base branch" in await worker.update_branch(tasks["open"], confirm=True) and updated == [31]
