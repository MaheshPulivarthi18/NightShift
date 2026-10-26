"""The repo workspace: one repo's issues and PRs, map, knowledge and numbers. Read-only.

Items are the repo's tasks (issues NightShift worked on, and PRs of yours it follows) and its PR checks
(incoming PRs). An item is closed once GitHub has merged or closed its PR, or its plan was rejected.
PR states come from one batched GraphQL query per repo, kept for a few minutes.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from nightshift.config import get_settings
from nightshift.context import archmap
from nightshift.db import PRCheck, RepoContext, Task, session_scope
from nightshift.github import GitHubClient, GitHubError
from nightshift.web.api import _environment_view, _iso, _repo
from nightshift.web.tasks_api import STAGES
from nightshift.worker.setup_agent import ENVIRONMENT, Environment, load_recipe

router = APIRouter(prefix="/api")

PR_STATES_SECONDS = 300
STATS_SECONDS = 3600
_pr_states: dict[tuple[str, int], tuple[float, str]] = {}  # (repo, number) -> (fetched at, OPEN|MERGED|CLOSED)
_stats: dict[str, tuple[float, dict[str, Any]]] = {}


def _pr_number(url: str | None) -> int | None:
    m = re.search(r"/pull/(\d+)", url or "")
    return int(m.group(1)) if m else None


async def pr_states(repo: str, numbers: list[int]) -> dict[int, str]:
    """GitHub's state of each PR: OPEN, MERGED or CLOSED. Unknown ones are left out (shown as open)."""
    now = time.monotonic()
    fresh = {n: s for n in numbers if (hit := _pr_states.get((repo, n))) and now - hit[0] < PR_STATES_SECONDS
             for s in [hit[1]]}  # fmt: skip
    missing = [n for n in dict.fromkeys(numbers) if n not in fresh]
    if missing:
        owner, name = repo.split("/", 1)
        fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ state }}" for n in missing)
        gh = GitHubClient()
        try:
            query = f"query($o: String!, $n: String!) {{ repository(owner: $o, name: $n) {{ {fields} }} }}"
            data = await gh.graphql(query, {"o": owner, "n": name})
        except GitHubError:
            return fresh  # GitHub is unreachable: show what is known, the rest as open
        finally:
            await gh.aclose()
        for n in missing:
            if pr := ((data.get("repository") or {}).get(f"p{n}")):
                _pr_states[(repo, n)] = (now, pr["state"])
                fresh[n] = pr["state"]
    return fresh


def _task_item(t: Task, state: str | None) -> dict[str, Any]:
    stage = STAGES.get(t.status, "stopped")
    res = t.result or {}
    pr_url = res.get("pr_url")
    closed = state in ("MERGED", "CLOSED") or stage == "closed"
    return {
        "kind": "task", "id": t.id, "number": t.issue_number, "title": (t.issue or {}).get("title", ""),
        "role": "yours" if pr_url else "issue", "status": t.status, "stage": stage,
        "pr_url": pr_url, "pr_state": state, "open": not closed, "hidden": t.archived_at is not None,
        "rehearsal": t.rehearsal, "updated_at": _iso(t.updated_at),
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
    task_prs = {t.id: _pr_number((t.result or {}).get("pr_url")) for t in tasks}
    numbers = [n for n in task_prs.values() if n] + [c.number for c in checks]
    states = await pr_states(repo.full_name, numbers) if numbers else {}
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
    env = next((c for c in contexts if c.kind == ENVIRONMENT), None)
    amap = next((c for c in contexts if c.kind == archmap.KIND), None)
    return {
        "environment": _environment_view(Environment.model_validate(env.data), env.created_at) if env else None,
        "recipe": await load_recipe(repo.id),
        "map": {"commit": amap.commit, "built_at": _iso(amap.created_at),
                "components": len(amap.data.get("components", [])),
                "relationships": len(amap.data.get("relationships", []))} if amap else None,
        "brief": (brief or {}).get("brief"),
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
