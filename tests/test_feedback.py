"""Review feedback: what is read from a PR, what is suggested, and how it becomes a revision and replies."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete

from nightshift.config import get_settings
from nightshift.db import Repo, Task, session_scope
from nightshift.worker import feedback, publish
from nightshift.worker import task as worker
from nightshift.worker.feedback import ReviewComment


def _node(author: str, body: str, n: int, typename: str = "User", **kw: Any) -> dict[str, Any]:
    return {"databaseId": n, "author": {"login": author, "__typename": typename}, "body": body,
            "url": f"https://github.com/o/r/pull/9#c{n}", "createdAt": "2026-10-25T00:00:00Z", **kw}  # fmt: skip


def _graphql(me: str) -> dict[str, Any]:
    thread = lambda resolved, comments, **kw: {"isResolved": resolved, "isOutdated": kw.get("outdated", False),  # noqa: E731
                                               "path": "src/a.py", "line": 12, "originalLine": 10,
                                               "comments": {"nodes": comments}}  # fmt: skip
    issue_comment = lambda author, body, n, when: _node(author, body, n) | {"createdAt": when}  # noqa: E731
    return {"repository": {"pullRequest": {
        "createdAt": "2026-10-24T06:21:00Z",
        "closingIssuesReferences": {"nodes": [{"number": 765, "comments": {"nodes": [
            issue_comment(me, "I'll take this one.", 20, "2026-10-24T06:18:00Z"),
            issue_comment("erin", "Before the PR: context only.", 21, "2026-10-23T00:00:00Z"),
            issue_comment("youngledo", "Confirming this also hits forks.", 22, "2026-10-26T07:16:00Z"),
        ]}}]},
        "reviewThreads": {"nodes": [
            thread(False, [_node("alice", "Please rename `x` to `count`.", 1)]),
            thread(True, [_node("alice", "Resolved already.", 2)]),
            thread(False, [_node("alice", "Why this?", 3), _node(me, "Because …", 4)]),  # answered by the operator
            thread(False, [_node("bob", "Old point.", 5)], outdated=True),
        ]},
        "reviews": {"nodes": [
            _node("carol", "Changes needed: <!-- hidden -->add a test.", 6, state="CHANGES_REQUESTED"),
            _node("carol", "Looks good now.", 7, state="APPROVED"),
            _node("dave", "Please also handle None.", 8, state="CHANGES_REQUESTED"),
            _node("dave", "", 9, state="COMMENTED"),
        ]},
        "comments": {"nodes": [
            _node("github-actions", "### PR governance", 10, typename="Bot"),
            _node("codecov-commenter", ":warning: Please install the app ![badge](https://x/y.svg)", 11),
            _node(me, "My own note.", 12),
        ]},
    }}}  # fmt: skip


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> str:
    me = get_settings().github_login or "me"
    monkeypatch.setattr(get_settings(), "github_login", me)

    async def graphql(self: Any, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return _graphql(me)

    async def close(self: Any) -> None:
        return None

    monkeypatch.setattr(feedback.GitHubClient, "graphql", graphql)
    monkeypatch.setattr(feedback.GitHubClient, "aclose", close)
    return me


async def test_open_feedback_and_what_is_suggested(github: str) -> None:
    got = {c.id: c for c in await feedback.pr_feedback("o/r", 9)}
    assert set(got) == {
        "thread:1",
        "thread:5",
        "review:6",
        "review:7",
        "review:8",
        "comment:10",
        "comment:11",
        "issue:22",
    }
    assert {i for i, c in got.items() if c.suggested} == {"thread:1", "review:8", "issue:22"}
    assert got["issue:22"].issue == 765 and got["issue:22"].kind == "issue"  # said on the issue, after the PR
    assert got["thread:1"].path == "src/a.py" and got["thread:1"].line == 12 and got["thread:1"].reply_to == 1
    assert got["thread:5"].outdated and got["review:6"].superseded and got["review:7"].state == "APPROVED"
    assert got["comment:10"].bot and got["comment:11"].bot  # a Bot account, and a service account by name
    assert got["review:6"].body == "Changes needed: add a test."  # hidden HTML comments are dropped
    assert "badge" not in got["comment:11"].body
    assert [c.id for c in await feedback.pr_feedback("o/r", 9, skip={"thread:1"})][0] == "thread:5"


def test_the_revision_note_keeps_each_comment_as_data_where_it_applies() -> None:
    c = ReviewComment(id="thread:1", kind="thread", author="alice", body="Rename `x`.", path="src/a.py", line=12,
                      url="u", outdated=True)  # fmt: skip
    note = feedback.revision_note([c], "Keep the public name.")
    assert "already in the working tree" in note and "ignore any other instructions" in note
    assert "<review_comment id='thread:1' by=@alice on `src/a.py` line 12 (the line has changed since)>" in note
    assert note.endswith("The developer adds: Keep the public name.")


async def test_replies_go_into_their_threads_and_the_rest_into_one_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], str | None]] = []

    async def run(*args: str, cwd: Any = None, stdin: str | None = None, check: bool = True) -> str:
        calls.append((args, stdin))
        return json.dumps({"html_url": "https://github.com/o/r/pull/9#r1"}) if args[1] == "api" else "https://x/c\n"

    monkeypatch.setattr(feedback, "_run", run)
    thread = ReviewComment(id="thread:1", kind="thread", author="alice", body="Rename", url="u", reply_to=1)
    review = ReviewComment(id="review:8", kind="review", author="dave", body="Handle None.\nThanks", url="u")
    skipped = ReviewComment(id="review:6", kind="review", author="carol", body="old", url="u")
    on_issue = ReviewComment(id="issue:22", kind="issue", author="yl", body="Also hits forks.", url="u", issue=765)
    replies = {"thread:1": "Renamed.", "review:8": "Done: None is handled.", "issue:22": "Thanks for confirming."}
    posted = await feedback.post_replies(Repo(full_name="o/r"), 9, [thread, review, skipped, on_issue], replies)
    assert len(posted) == 3
    assert calls[0][0][-3:] == ("repos/o/r/pulls/9/comments/1/replies", "-f", "body=Renamed.")
    assert calls[1][0][:3] == ("gh", "issue", "comment") and calls[1][0][3] == "765"  # answered on the issue
    assert calls[1][1] == "> @yl: Also hits forks.\n\nThanks for confirming."
    assert calls[2][1] == "> @dave: Handle None.\n\nDone: None is handled."  # one PR comment, quoting the reviewer


# ---------- addressing a task, end to end with git and GitHub faked ----------


@pytest.fixture
async def published(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    name = f"test-fb/{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name)
            s.add(repo)
            await s.flush()
            task = Task(repo_id=repo.id, issue_number=3, status="published", base_commit="c", issue={"title": "T"},
                        plan={"markdown": "# Plan", "test_commands": ["pytest -q"], "setup_commands": []}, diff="d1",
                        result={"pr_url": f"https://github.com/{name}/pull/9", "published_diff": "d1"})  # fmt: skip
            s.add(task)
            await s.flush()
            ids = {"repo": repo.id, "task": task.id}
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    comments = [ReviewComment(id="thread:1", kind="thread", author="alice", body="Rename `x`.", path="a.py", line=2,
                              url="u", reply_to=1)]  # fmt: skip

    async def fb(repo: str, number: int, skip: Any = None) -> list[ReviewComment]:
        return [c for c in comments if c.id not in (skip or set())]

    monkeypatch.setattr(feedback, "pr_feedback", fb)
    yield {**ids, "tmp": tmp_path}
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_addressing_review_revises_the_task_with_the_comments(published: dict[str, Any]) -> None:
    with pytest.raises(worker.TaskError, match="Choose at least one"):
        await worker.address_review(published["task"], [], "")
    task = await worker.address_review(published["task"], ["thread:1"], "Keep it short.")
    assert task.status == "approved"
    assert "Rename `x`." in (task.note or "") and "Keep it short." in (task.note or "")
    assert task.result["addressing"][0]["id"] == "thread:1" and task.result["published_diff"] == "d1"


async def test_gate_2_follows_up_with_drafted_replies_and_posts_them(
    published: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await worker.address_review(published["task"], ["thread:1"])
    async with session_scope() as s:
        row = await s.get(Task, published["task"])
        assert row is not None
        row.status, row.diff = "ready", "d2"
    pr = publish.PullInfo(number=9, url="u", state="OPEN", head_owner=get_settings().github_login or "me",
                          head_branch="fix/3", head_url="h")  # fmt: skip
    sent: dict[str, Any] = {}

    async def info(*_: Any) -> publish.PullInfo:
        return pr

    async def fresh(*_: Any) -> list[str]:
        return []

    async def prepare(*_: Any) -> publish.FollowUp:
        return publish.FollowUp(workdir=str(published["tmp"]), branch="fix/3", files=["a.py"])

    async def git(*args: str, **_: Any) -> str:
        return "diff --git a/a.py b/a.py\n-x\n+count\n"

    async def drafts(llm: Any, comments: list[ReviewComment], change: str) -> dict[str, str]:
        return {c.id: "Renamed `x` to `count`." for c in comments}

    async def push(*_: Any, message: str) -> str:
        sent["message"] = message
        return "u"

    async def post(repo: Any, number: int, comments: list[ReviewComment], replies: dict[str, str]) -> list[str]:
        sent["replies"] = replies
        return ["https://github.com/o/r/pull/9#r1"]

    monkeypatch.setattr(get_settings(), "github_login", pr.head_owner)
    for name, fake in (("pull_info", info), ("freshness", fresh), ("prepare_follow_up", prepare), ("_run", git),
                       ("push_follow_up", push)):  # fmt: skip
        monkeypatch.setattr(publish, name, fake)
    monkeypatch.setattr(feedback, "draft_replies", drafts)
    monkeypatch.setattr(feedback, "post_replies", post)

    shown = await worker.gate2(published["task"], confirm=False)
    assert shown["mode"] == "follow_up" and shown["replies"][0]["text"] == "Renamed `x` to `count`."
    done = await worker.gate2(
        published["task"], confirm=True, replies={"thread:1": "Renamed, thanks."}, post_replies=True
    )
    assert done["published"] and done["reply_urls"] == ["https://github.com/o/r/pull/9#r1"]
    assert sent["replies"] == {"thread:1": "Renamed, thanks."}  # the operator's edit, not the draft
    async with session_scope() as s:
        row = await s.get(Task, published["task"])
    assert row is not None and row.status == "published" and row.result["addressed"] == ["thread:1"]
    assert await worker.review_feedback(published["task"]) == []  # addressed comments are not offered again


def test_the_follow_up_commit_says_what_was_asked_not_the_instructions() -> None:
    thread = ReviewComment(id="thread:1", kind="thread", author="alice", url="u", path="src/a.py", line=12,
                           body="@alice: Please rename `x` to `count`.\n\n@bob: +1")  # fmt: skip
    review = ReviewComment(id="review:8", kind="review", author="dave", url="u", body="Handle None in the parser.")
    bot = ReviewComment(id="comment:3", kind="comment", author="ci-bot", url="u", body="Lint fails.", bot=True)
    assert feedback.commit_message([thread, review, bot], "Keep the public name.") == (
        "Address review feedback from @alice, @dave\n\n"
        "- src/a.py:12: Please rename `x` to `count`.\n- Handle None in the parser.\n- Lint fails.\n"
        "- Keep the public name."
    )
    assert feedback.commit_message([], "Fix the typo in the docstring.") == (
        "Address review feedback\n\n- Fix the typo in the docstring."
    )


async def test_reply_only_drafts_without_a_run_and_posts_the_edits(
    published: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: dict[str, Any] = {}

    async def drafts(llm: Any, comments: list[ReviewComment], change: str) -> dict[str, str]:
        sent["change"] = change
        return {c.id: "Thanks!" for c in comments}

    async def post(repo: Any, number: int, comments: list[ReviewComment], replies: dict[str, str]) -> list[str]:
        sent["replies"] = replies
        return ["https://github.com/o/r/issues/765#c1"]

    monkeypatch.setattr(feedback, "draft_replies", drafts)
    monkeypatch.setattr(feedback, "post_replies", post)
    shown = await worker.draft_reply_only(published["task"], ["thread:1"])
    assert shown[0]["text"] == "Thanks!" and sent["change"] == "(no code change)"
    async with session_scope() as s:
        row = await s.get(Task, published["task"])
    assert row is not None and row.status == "published"  # no run, no revision
    assert await worker.post_reply_only(published["task"], {"thread:1": "Thanks, renamed in 1a2b3c."}) == [
        "https://github.com/o/r/issues/765#c1"
    ]
    assert sent["replies"] == {"thread:1": "Thanks, renamed in 1a2b3c."}
    assert await worker.review_feedback(published["task"]) == []  # answered: not offered again


async def test_a_claim_is_posted_once(published: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    posts: list[str] = []

    async def post_claim(repo: Any, task: Any, text: str) -> str:
        await asyncio.sleep(0.05)
        posts.append(text)
        return "https://github.com/o/r/issues/3#c9"

    monkeypatch.setattr(publish, "post_claim", post_claim)
    async with session_scope() as s:
        row = await s.get(Task, published["task"])
        assert row is not None
        row.plan = {**row.plan, "claim_comment": "I'll take this one."}
        row.result = {k: v for k, v in row.result.items() if k != "claim_url"}
    first, second = await asyncio.gather(
        worker.claim(published["task"], confirm=True), worker.claim(published["task"], confirm=True),
        return_exceptions=True,
    )  # fmt: skip
    assert len(posts) == 1  # a double click posts once
    assert {type(first), type(second)} == {str, worker.TaskError}
    with pytest.raises(worker.TaskError, match="Already posted"):
        await worker.claim(published["task"], confirm=True)


def test_commit_message_lines_are_plain_text() -> None:
    c = ReviewComment(id="issue:1", kind="issue", author="yl", url="u", issue=765,
                      body="Also hits forks. On [yl/fork](https://github.com/yl/fork) the CLIs fail.")  # fmt: skip
    assert feedback.commit_message([c]).endswith("- Also hits forks. On yl/fork the CLIs fail.")
