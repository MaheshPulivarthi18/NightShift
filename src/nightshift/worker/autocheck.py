"""Incoming pull requests on repos you maintain, checked without being asked.

A repo opts in (`auto_check`, set on the Repos page). On every background sync, its open PRs by others are
listed, newest first, and each whose latest commit has no check yet is checked, a few per repo per sync
(`auto_check_per_sync`) so the cost stays bounded. Drafts and bots are left out. A finished check waits in
"Needs you" until you post it as a review (your click, your gh login) or dismiss it. Nothing is posted here.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select

from nightshift.config import get_settings
from nightshift.db import PRCheck, Repo, session_scope
from nightshift.github import GitHubClient, GitHubError

log = structlog.get_logger()

status: dict[str, Any] = {"running": False, "checked": 0, "error": None}


def wanted(pulls: list[dict[str, Any]], checked: set[tuple[int, str]], me: str) -> list[dict[str, Any]]:
    """The open PRs to check: by someone else, not a draft or a bot, at a commit not checked yet."""
    out = []
    for p in pulls:
        user = p.get("user") or {}
        login = (user.get("login") or "").lower()
        if p.get("draft") or user.get("type") == "Bot" or login.endswith("[bot]") or (me and login == me):
            continue
        if (p["number"], (p.get("head") or {}).get("sha", "")) in checked:
            continue
        out.append(p)
    return out


async def open_pulls(full_name: str) -> list[dict[str, Any]]:
    gh = GitHubClient()
    try:
        pulls = await gh.rest_get(f"/repos/{full_name}/pulls?state=open&sort=created&direction=desc&per_page=30")
    finally:
        await gh.aclose()
    return list(pulls or [])


async def autocheck_once() -> dict[str, Any]:
    """Check new incoming PRs on every repo that opted in. One at a time: each runs in a sandbox."""
    from nightshift.worker.prcheck import check_pr  # it imports the task module, which imports the sync

    if status["running"]:
        return status
    settings = get_settings()
    me = (settings.github_login or "").lower()
    status.update(running=True, checked=0, error=None)
    try:
        async with session_scope() as s:
            repos = (await s.execute(select(Repo).where(Repo.auto_check, Repo.watched))).scalars().all()
        for repo in repos:
            try:
                pulls = await open_pulls(repo.full_name)
            except GitHubError as e:
                log.warning("autocheck_list_failed", repo=repo.full_name, error=str(e)[:200])
                continue
            async with session_scope() as s:
                rows = (await s.execute(select(PRCheck).where(PRCheck.repo_id == repo.id))).scalars().all()
            done = {(c.number, c.head_sha) for c in rows}
            for p in wanted(pulls, done, me)[: settings.auto_check_per_sync]:
                try:
                    await check_pr(p["html_url"], auto=True)
                    status["checked"] += 1
                except Exception as e:  # recorded on the check row; one PR must not stop the others
                    log.warning("autocheck_failed", url=p["html_url"], error=str(e)[:200])
    except Exception as e:  # never take the sync loop down
        log.exception("autocheck_crashed")
        status["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        status["running"] = False
    return status
