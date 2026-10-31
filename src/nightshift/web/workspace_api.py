"""The repo workspace: one repo's issues and PRs, map, knowledge and numbers. Read-only.

Items are the repo's tasks (issues NightShift worked on, and PRs of yours it follows) and its PR checks
(incoming PRs). An item is closed once GitHub has merged or closed its PR, or its plan was rejected.
PR states come from one batched GraphQL query per repo, kept for a few minutes.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from nightshift import events, github
from nightshift.config import get_settings
from nightshift.context import archmap
from nightshift.db import PRCheck, RepoContext, Run, Task, session_scope
from nightshift.github import GitHubClient, GitHubError
from nightshift.web.api import _environment_view, _iso, _repo
from nightshift.web.common import Guard
from nightshift.web.tasks_api import STAGES, ci_failing
from nightshift.worker import sync
from nightshift.worker.onboard import BRIEF
from nightshift.worker.setup_agent import ENVIRONMENT, Environment, load_recipe

router = APIRouter(prefix="/api")

STATS_SECONDS = 3600
_stats: dict[str, tuple[float, dict[str, Any]]] = {}


def _task_item(t: Task, state: str | None) -> dict[str, Any]:
    stage = STAGES.get(t.status, "stopped")
    res = t.result or {}
    pr_url = res.get("pr_url")
    closed = state in ("MERGED", "CLOSED") or stage == "closed"
    return {
        "kind": "task", "id": t.id, "number": t.issue_number, "title": (t.issue or {}).get("title", ""),
        "pr_number": sync.pr_number(pr_url),
        "role": "yours" if pr_url else "issue", "status": t.status, "stage": stage,
        "pr_url": pr_url, "pr_state": state, "open": not closed, "hidden": t.archived_at is not None,
        "updated_at": _iso(t.updated_at),
        "feedback": len(((res.get("sync") or {}).get("open")) or [])
        if state == "OPEN" or (res.get("sync") or {}).get("kind") == "answers"
        else 0,
        "ci_failing": ci_failing(res.get("sync")),
        "asking": t.status == "planned" and not (t.plan or {}).get("actionable", True),
        "asked": str(res.get("claim_url") or "").startswith("http"),
    }  # fmt: skip


def _check_item(c: PRCheck, state: str | None, me: str) -> dict[str, Any]:
    return {
        "kind": "check", "id": c.id, "number": c.number, "title": c.title,
        "role": "yours" if me and c.author.lower() == me else "incoming", "status": c.status,
        "verdict": c.verdict, "must": c.must, "should": c.should, "pr_url": c.url, "pr_state": state,
        "open": state not in ("MERGED", "CLOSED"), "hidden": False, "updated_at": _iso(c.created_at),
    }  # fmt: skip


@router.get("/repos/{owner}/{name}/items")
async def repo_items(owner: str, name: str) -> list[dict[str, Any]]:
    """The repo's tasks and PR checks, newest first, with their PRs' state on GitHub."""
    repo = await _repo(f"{owner}/{name}")
    async with session_scope() as s:
        tasks = list((await s.execute(select(Task).where(Task.repo_id == repo.id).order_by(Task.id.desc()))).scalars())
        checks = list(
            (await s.execute(select(PRCheck).where(PRCheck.repo_id == repo.id).order_by(PRCheck.id.desc()))).scalars()
        )
    task_prs = {t.id: sync.pr_number((t.result or {}).get("pr_url")) for t in tasks}
    numbers = [n for n in task_prs.values() if n] + [c.number for c in checks]
    states = await sync.pr_states(repo.full_name, numbers) if numbers else {}
    me = (get_settings().github_login or "").lower()
    items = [_task_item(t, states.get(n) if (n := task_prs[t.id]) else None) for t in tasks]
    items += [_check_item(c, states.get(c.number), me) for c in checks]
    return sorted(items, key=lambda i: i["updated_at"] or "", reverse=True)


@router.get("/repos/{owner}/{name}/map")
async def repo_map(owner: str, name: str) -> dict[str, Any]:
    """The newest architecture map: components (with the file behind each) and how they connect."""
    repo = await _repo(f"{owner}/{name}")
    async with session_scope() as s:
        row = (
            await s.execute(
                select(RepoContext)
                .where(RepoContext.repo_id == repo.id, RepoContext.kind == archmap.KIND)
                .order_by(RepoContext.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"No map for {repo.full_name} yet")
    return {"commit": row.commit, "built_at": _iso(row.created_at), **row.data}


@router.get("/repos/{owner}/{name}/knowledge")
async def repo_knowledge(owner: str, name: str) -> dict[str, Any]:
    """What NightShift has learned about the repo once and reuses: environment, commands, map and brief."""
    repo = await _repo(f"{owner}/{name}")
    await events.mark_interrupted_runs()  # a setup whose server died shows as interrupted, not running forever
    async with session_scope() as s:
        contexts = list(
            (
                await s.execute(
                    select(RepoContext).where(RepoContext.repo_id == repo.id).order_by(RepoContext.created_at.desc())
                )
            ).scalars()
        )
        brief = (
            await s.execute(
                select(Task.result)
                .where(Task.repo_id == repo.id, Task.result["brief"].is_not(None))
                .order_by(Task.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        setup = (
            await s.execute(
                select(Run).where(Run.run_id.like(f"setup-{repo.id}-%")).order_by(Run.started_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    env = next((c for c in contexts if c.kind == ENVIRONMENT), None)
    amap = next((c for c in contexts if c.kind == archmap.KIND), None)
    stored_brief = next((c for c in contexts if c.kind == BRIEF), None)
    return {
        "environment": _environment_view(Environment.model_validate(env.data), env.created_at) if env else None,
        "recipe": await load_recipe(repo.id),
        "map": {"commit": amap.commit, "built_at": _iso(amap.created_at),
                "components": len(amap.data.get("components", [])),
                "relationships": len(amap.data.get("relationships", []))} if amap else None,
        "brief": stored_brief.data.get("text") if stored_brief else (brief or {}).get("brief"),
        "setup": {"run_id": setup.run_id, "status": setup.status, "step": setup.current_node, "error": setup.error,
                  "at": _iso(setup.started_at)} if setup else None,
    }  # fmt: skip


@router.get("/repos/{owner}/{name}/stats")
async def repo_stats(owner: str, name: str) -> dict[str, Any]:
    """Open issues and PRs, PRs merged in the last 30 days, and stars, from GitHub; kept for an hour."""
    repo = await _repo(f"{owner}/{name}")
    now = time.monotonic()
    if (hit := _stats.get(repo.full_name)) and now - hit[0] < STATS_SECONDS:
        return hit[1]
    since = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
    query = """query($o: String!, $n: String!, $merged: String!) {
      repository(owner: $o, name: $n) { stargazerCount issues(states: OPEN) { totalCount }
                                        pullRequests(states: OPEN) { totalCount } }
      merged: search(query: $merged, type: ISSUE) { issueCount } }"""
    gh = GitHubClient()
    try:
        data = await gh.graphql(
            query, {"o": owner, "n": name, "merged": f"repo:{repo.full_name} is:pr is:merged merged:>={since}"}
        )
    except GitHubError as e:
        raise HTTPException(502, str(e)) from e
    finally:
        await gh.aclose()
    r = data.get("repository") or {}
    out = {
        "open_issues": (r.get("issues") or {}).get("totalCount"),
        "open_prs": (r.get("pullRequests") or {}).get("totalCount"),
        "merged_30d": (data.get("merged") or {}).get("issueCount"),
        "stars": r.get("stargazerCount"),
    }
    _stats[repo.full_name] = (now, out)
    return out


@router.get("/sync")
async def sync_status() -> dict[str, Any]:
    """When GitHub was last checked for feedback on your published PRs, and what was found."""
    return {**sync.status, "minutes": get_settings().sync_minutes, "github_down_since": github.outage["since"]}


@router.post("/sync", dependencies=[Guard])
async def sync_now() -> dict[str, Any]:
    """Check now, in the background; the status says when it is done."""
    if not sync.status["running"]:
        asyncio.create_task(sync.sync_once())
    return {**sync.status, "running": True, "minutes": get_settings().sync_minutes}
