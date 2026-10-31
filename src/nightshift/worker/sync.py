"""Background sync: what changed on GitHub for your published PRs, so new feedback reaches Needs you.

It runs when the app starts and then every `sync_minutes` (0: only when asked, with Sync now). For each
published task whose PR is still open, it reads the PR's open feedback the way the task page does,
leaving out what you already answered, here or on GitHub, and the CI on the PR's latest commit (a red PR
waits on its author: maintainers rarely review one), and keeps a short summary on the task. Once the PR is
merged, it notes whether the issue it fixed is still open, so a "fixed by" comment can be offered. For a
plan that asked the maintainers first, it reads the issue for replies after your question. Read-only:
nothing is posted.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, text

from nightshift.config import get_settings
from nightshift.db import Repo, Task, session_scope
from nightshift.github import ADVISORY_JOB, GitHubClient, GitHubError, ci_status
from nightshift.worker.feedback import SERVICE

log = structlog.get_logger()

PR_STATES_SECONDS = 300
_pr_states: dict[tuple[str, int], tuple[float, str]] = {}  # (repo, number) -> (fetched at, OPEN|MERGED|CLOSED)
_pr_heads: dict[tuple[str, int], str] = {}  # (repo, number) -> the PR's latest commit, from the same query
status: dict[str, Any] = {"at": None, "running": False, "checked": 0, "with_feedback": 0, "error": None}


def pr_number(url: str | None) -> int | None:
    m = re.search(r"/pull/(\d+)", url or "")
    return int(m.group(1)) if m else None


async def pr_states(repo: str, numbers: list[int], *, fresh: bool = False) -> dict[int, str]:
    """GitHub's state of each PR: OPEN, MERGED or CLOSED, in one query per repo, kept a few minutes.
    PRs GitHub could not be asked about are left out (shown as open)."""
    now = time.monotonic()
    known: dict[int, str] = {}
    for n in numbers:
        hit = _pr_states.get((repo, n))
        if hit and not fresh and now - hit[0] < PR_STATES_SECONDS:
            known[n] = hit[1]
    missing = [n for n in dict.fromkeys(numbers) if n not in known]
    if not missing:
        return known
    owner, name = repo.split("/", 1)
    fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ state headRefOid }}" for n in missing)
    gh = GitHubClient()
    try:
        query = f"query($o: String!, $n: String!) {{ repository(owner: $o, name: $n) {{ {fields} }} }}"
        data = await gh.graphql(query, {"o": owner, "n": name})
    except GitHubError as e:
        log.warning("pr_states_failed", repo=repo, error=str(e)[:200])
        return known
    finally:
        await gh.aclose()
    for n in missing:
        if pr := (data.get("repository") or {}).get(f"p{n}"):
            _pr_states[(repo, n)] = (now, pr["state"])
            if pr.get("headRefOid"):
                _pr_heads[(repo, n)] = pr["headRefOid"]
            known[n] = pr["state"]
    return known


async def pr_ci(repo: str, number: int) -> dict[str, Any] | None:
    """The CI on an open PR's latest commit: {"state": failed | pending | passed | none, "failed": [jobs]}.
    Benchmark and coverage jobs are advice, not a failure. None when the commit is not known."""
    head = _pr_heads.get((repo, number))
    if not head:
        return None
    gh = GitHubClient()
    try:
        ci = await ci_status(gh, repo, head)
    finally:
        await gh.aclose()
    failed = [j for j in ci["failed"] if not ADVISORY_JOB.search(j)]
    state = "failed" if failed else "pending" if ci["pending"] else "passed" if ci["passed"] else "none"
    return {"state": state, "failed": failed[:8], "sha": head[:7]}


async def issue_open(repo: str, number: int) -> bool | None:
    """Whether the issue is still open (None: GitHub did not say). A merged PR without "Fixes #N" leaves it open."""
    gh = GitHubClient()
    try:
        issue = await gh.rest_get(f"/repos/{repo}/issues/{number}")
    except GitHubError as e:
        log.warning("sync_issue_failed", repo=repo, issue=number, error=str(e)[:200])
        return None
    finally:
        await gh.aclose()
    return issue.get("state") == "open" and "pull_request" not in issue


async def _save_sync(task_id: int, summary: dict[str, Any]) -> None:
    """Set result.sync without rewriting the rest of the result, which a running task may be changing."""
    async with session_scope() as s:
        await s.execute(
            text(
                "update tasks set result = coalesce(result, '{}'::jsonb) "
                "|| jsonb_build_object('sync', cast(:v as jsonb)) where id = :id"
            ),
            {"v": json.dumps(summary), "id": task_id},
        )  # fmt: skip


async def sync_once(repos: list[str] | None = None) -> dict[str, Any]:
    """Check every published task's PR once (only those in `repos`, if given). Returns the sync status."""
    from nightshift.worker.task import TaskError, review_feedback  # the task module imports this one

    if status["running"]:
        return status
    status.update(running=True, error=None)
    checked = with_feedback = 0
    try:
        query = select(Task, Repo).join(Repo).where(Task.status == "published", Task.archived_at.is_(None))
        if repos is not None:
            query = query.where(Repo.full_name.in_(repos))
        async with session_scope() as s:
            rows = (await s.execute(query)).all()
        by_repo: dict[str, list[tuple[Task, int]]] = defaultdict(list)
        for t, r in rows:
            if n := pr_number((t.result or {}).get("pr_url")):
                by_repo[r.full_name].append((t, n))
        for repo, tasks in by_repo.items():
            states = await pr_states(repo, [n for _, n in tasks], fresh=True)
            for t, n in tasks:
                state = states.get(n)
                comments = []
                ci = None
                if state == "OPEN":
                    try:
                        comments = await review_feedback(t.id)
                    except TaskError as e:
                        log.warning("sync_feedback_failed", task=t.id, error=str(e)[:200])
                        continue
                    ci = await pr_ci(repo, n)
                open_ = [
                    {"id": c.id, "kind": c.kind, "author": c.author, "url": c.url,
                     "text": next((ln for ln in c.body.splitlines() if ln.strip()), "")[:200]}
                    for c in comments if c.suggested
                ]  # fmt: skip
                summary: dict[str, Any] = {
                    "at": datetime.now(UTC).isoformat(),
                    "pr_state": state,
                    "open": open_,
                    "ci": ci,
                }
                if state == "MERGED" and not (t.result or {}).get("fixed_comment_url"):
                    before = ((t.result or {}).get("sync") or {}).get("issue_open")
                    # a closed issue stays closed: asked about until then, not every sync forever
                    summary["issue_open"] = False if before is False else await issue_open(repo, t.issue_number)
                await _save_sync(t.id, summary)
                checked += 1
                with_feedback += bool(open_)
        checked_q, answered = await _sync_questions(repos)
        checked += checked_q
        with_feedback += answered
    except Exception as e:  # a failed sync must never take the app down; the next one tries again
        log.exception("sync_failed")
        status["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        status.update(running=False, at=datetime.now(UTC).isoformat(), checked=checked, with_feedback=with_feedback)
    return status


async def _sync_questions(repos: list[str] | None) -> tuple[int, int]:
    """Plans that asked on the issue first: replies from others since your last comment there."""
    from nightshift.worker.issue import fetch_issue

    query = select(Task, Repo).join(Repo).where(Task.status == "planned", Task.archived_at.is_(None))
    if repos is not None:
        query = query.where(Repo.full_name.in_(repos))
    async with session_scope() as s:
        rows = (await s.execute(query)).all()
    me = (get_settings().github_login or "").lower()
    checked = answered = 0
    for t, r in rows:
        url = (t.result or {}).get("claim_url") or ""
        if (t.plan or {}).get("actionable", True) or not url.startswith("http"):
            continue  # nothing was asked, or the question is not posted yet
        try:
            issue = await fetch_issue(r.full_name, t.issue_number)
        except GitHubError as e:
            log.warning("sync_question_failed", task=t.id, error=str(e)[:200])
            continue
        mine = max((c["created_at"] for c in issue.comments if me and c["author"].lower() == me), default="")
        replies = [
            {"id": c["created_at"], "kind": "issue", "author": c["author"], "url": issue.url,
             "text": next((ln for ln in c["body"].splitlines() if ln.strip()), "")[:200]}
            for c in issue.comments
            if c["author"].lower() != me and c["created_at"] > mine and not SERVICE.search(c["author"])
        ]  # fmt: skip
        await _save_sync(t.id, {"at": datetime.now(UTC).isoformat(), "kind": "answers", "pr_state": None,
                                "issue_state": issue.state, "open": replies})  # fmt: skip
        checked += 1
        answered += bool(replies)
    return checked, answered


async def sync_loop() -> None:
    """Sync now and then every `sync_minutes`; with 0, only when asked. Each round also checks new incoming
    PRs on the repos that opted in."""
    minutes = get_settings().sync_minutes
    if minutes <= 0:
        return
    from nightshift.worker.autocheck import autocheck_once

    while True:
        await sync_once()
        await autocheck_once()  # incoming PRs on repos that opted in
        await asyncio.sleep(minutes * 60)
