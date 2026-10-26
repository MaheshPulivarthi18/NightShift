"""Background sync: what changed on GitHub for your published PRs, so new feedback reaches Needs you.

It runs when the app starts and then every `sync_minutes` (0: only when asked, with Sync now). For each
published task whose PR is still open, it reads the PR's open feedback the way the task page does,
leaving out what you already answered, here or on GitHub, and keeps a short summary on the task.
Read-only: nothing is posted.
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
from nightshift.github import GitHubClient, GitHubError

log = structlog.get_logger()

PR_STATES_SECONDS = 300
_pr_states: dict[tuple[str, int], tuple[float, str]] = {}  # (repo, number) -> (fetched at, OPEN|MERGED|CLOSED)
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
    fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ state }}" for n in missing)
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
            known[n] = pr["state"]
    return known


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
                if state == "OPEN":
                    try:
                        comments = await review_feedback(t.id)
                    except TaskError as e:
                        log.warning("sync_feedback_failed", task=t.id, error=str(e)[:200])
                        continue
                open_ = [
                    {"id": c.id, "kind": c.kind, "author": c.author, "url": c.url,
                     "text": next((ln for ln in c.body.splitlines() if ln.strip()), "")[:200]}
                    for c in comments if c.suggested
                ]  # fmt: skip
                await _save_sync(t.id, {"at": datetime.now(UTC).isoformat(), "pr_state": state, "open": open_})
                checked += 1
                with_feedback += bool(open_)
    except Exception as e:  # a failed sync must never take the app down; the next one tries again
        log.exception("sync_failed")
        status["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        status.update(running=False, at=datetime.now(UTC).isoformat(), checked=checked, with_feedback=with_feedback)
    return status


async def sync_loop() -> None:
    """Sync now and then every `sync_minutes`; with 0, only when asked."""
    minutes = get_settings().sync_minutes
    if minutes <= 0:
        return
    while True:
        await sync_once()
        await asyncio.sleep(minutes * 60)
