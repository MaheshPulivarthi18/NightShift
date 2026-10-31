"""The track record: what happened to the pull requests made with NightShift.

Every task with a pull request counts: its state on GitHub (merged, approved and waiting, changes requested,
waiting for a first review, closed without merging), how long it took to merge, and what it cost. A pull
request made before its task was kept (or opened by hand) can be added: it is stored as an archived task, so it
counts here but never shows on a board or in "Needs you".
"""

from __future__ import annotations

import re
import statistics
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from nightshift.config import get_settings
from nightshift.db import Repo, Task, session_scope
from nightshift.github import GitHubClient, GitHubError
from nightshift.memory import get_or_create_repo
from nightshift.worker.sync import pr_number

log = structlog.get_logger()

FACTS_SECONDS = 300
_facts: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
PR_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?$")


class RecordError(RuntimeError):
    pass


async def pr_facts(repo: str, numbers: list[int]) -> dict[int, dict[str, Any]]:
    """State, dates and reviews of each PR, in one query per repo, kept a few minutes."""
    now = time.monotonic()
    known: dict[int, dict[str, Any]] = {}
    missing = []
    for n in numbers:
        hit = _facts.get((repo, n))
        if hit and now - hit[0] < FACTS_SECONDS:
            known[n] = hit[1]
        else:
            missing.append(n)
    if not missing:
        return known
    owner, name = repo.split("/", 1)
    fields = " ".join(
        f"p{n}: pullRequest(number: {n}) {{ title state createdAt mergedAt closedAt author {{ login }} "
        "reviews(last: 50) { nodes { state author { login } } } "
        "commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } }"
        for n in missing
    )
    gh = GitHubClient()
    try:
        query = f"query($o: String!, $n: String!) {{ repository(owner: $o, name: $n) {{ {fields} }} }}"
        data = await gh.graphql(query, {"o": owner, "n": name})
    except GitHubError as e:  # the record shows what it has; a repo GitHub would not answer is marked unknown
        log.warning("pr_facts_failed", repo=repo, error=str(e)[:200])
        return known
    finally:
        await gh.aclose()
    for n in missing:
        pr = (data.get("repository") or {}).get(f"p{n}")
        if pr:
            facts = _facts_of(pr)
            _facts[(repo, n)] = (now, facts)
            known[n] = facts
    return known


def _facts_of(pr: dict[str, Any]) -> dict[str, Any]:
    author = (pr.get("author") or {}).get("login")
    latest: dict[str, str] = {}  # each reviewer's latest approval or change request
    for r in (pr.get("reviews") or {}).get("nodes", []):
        who = (r.get("author") or {}).get("login")
        if who and who != author and r.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
            latest[who] = r["state"]
    if pr["state"] == "MERGED":
        outcome = "merged"
    elif pr["state"] == "CLOSED":
        outcome = "closed"
    elif "CHANGES_REQUESTED" in latest.values():
        outcome = "changes requested"
    elif "APPROVED" in latest.values():
        outcome = "approved"
    else:
        outcome = "waiting"
    commits = (pr.get("commits") or {}).get("nodes") or [{}]
    rollup = ((commits[-1].get("commit") or {}).get("statusCheckRollup") or {}).get("state")
    ci = {"SUCCESS": "passing", "FAILURE": "failing", "ERROR": "failing", "PENDING": "running",
          "EXPECTED": "running"}.get(rollup or "", "none")  # fmt: skip
    return {"title": pr.get("title", ""), "outcome": outcome, "created_at": pr.get("createdAt"),
            "merged_at": pr.get("mergedAt"), "closed_at": pr.get("closedAt"), "ci": ci}  # fmt: skip


def _days(start: str | None, end: str | None) -> float | None:
    if not start:
        return None
    a = datetime.fromisoformat(start.replace("Z", "+00:00"))
    b = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else datetime.now(UTC)
    return round((b - a).total_seconds() / 86400, 1)


OUTCOMES = ("merged", "approved", "changes requested", "waiting", "closed", "unknown")


async def track_record() -> dict[str, Any]:
    """Every pull request made with NightShift, newest first, and the totals."""
    async with session_scope() as s:
        rows = (await s.execute(select(Task, Repo).join(Repo).order_by(Task.id.desc()))).all()
    prs = [(t, r, n) for t, r in rows if (n := pr_number((t.result or {}).get("pr_url")))]
    by_repo: dict[str, list[int]] = {}
    for _, r, n in prs:
        by_repo.setdefault(r.full_name, []).append(n)
    facts: dict[tuple[str, int], dict[str, Any]] = {}
    for repo, numbers in by_repo.items():
        for n, f in (await pr_facts(repo, numbers)).items():
            facts[(repo, n)] = f
    out = []
    for t, r, n in prs:
        f = facts.get((r.full_name, n)) or {"outcome": "unknown", "title": "", "created_at": None}
        u = ((t.result or {}).get("understanding") or {}).get("attempts") or []
        out.append({
            "repo": r.full_name, "number": n, "url": t.result["pr_url"], "task_id": t.id,
            "title": f.get("title") or (t.issue or {}).get("title", ""), "outcome": f["outcome"],
            "opened": f.get("created_at"), "days_to_merge": _days(f.get("created_at"), f.get("merged_at"))
            if f.get("merged_at") else None,
            "days_open": _days(f.get("created_at"), None) if f["outcome"] in ("approved", "changes requested",
                                                                                "waiting") else None,
            "cost_usd": round(t.cost_usd or 0.0, 2), "imported": bool((t.result or {}).get("imported")),
            "ci": f.get("ci", "none"),
            "attempts": len((t.result or {}).get("attempts", [])) + 1,
            "understood": bool(u and u[-1].get("passed")) if u else None,
        })  # fmt: skip
    return {"summary": summary(out), "prs": out}


def summary(prs: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {o: sum(p["outcome"] == o for p in prs) for o in OUTCOMES}
    # an open pull request whose CI fails needs its author: maintainers rarely review a red one
    ci_failing = sum(p.get("ci") == "failing" and p["outcome"] in ("approved", "changes requested", "waiting")
                     for p in prs)  # fmt: skip
    merge_days = [p["days_to_merge"] for p in prs if p["days_to_merge"] is not None]
    costs = [p["cost_usd"] for p in prs if not p["imported"]]  # imported PRs have no recorded cost
    merged_cost = [p["cost_usd"] for p in prs if p["outcome"] == "merged" and not p["imported"]]
    decided = counts["merged"] + counts["closed"]
    return {
        "prs": len(prs),
        **{o.replace(" ", "_"): c for o, c in counts.items()},
        "accepted": counts["merged"] + counts["approved"],
        "merge_rate": round(counts["merged"] / decided, 2) if decided else None,
        "median_days_to_merge": round(statistics.median(merge_days), 1) if merge_days else None,
        "median_cost_usd": round(statistics.median(costs), 2) if costs else None,
        "total_cost_usd": round(sum(costs), 2),
        "cost_per_merged_usd": round(sum(merged_cost) / len(merged_cost), 2) if merged_cost else None,
        "with_cost": len(costs),
        "ci_failing": ci_failing,
    }


async def add_pr(url: str) -> Task:
    """Count a pull request of yours made before its task was kept, or opened by hand: an archived task, so it
    shows here and nowhere else. Open, merged or closed."""
    m = PR_URL.match(url.strip())
    if not m:
        raise RecordError("Give a pull request link, like https://github.com/owner/repo/pull/123.")
    full_name, number = m.group(1), int(m.group(2))
    gh = GitHubClient()
    try:
        pr = await gh.rest_get(f"/repos/{full_name}/pulls/{number}")
    except GitHubError as e:
        raise RecordError(f"Cannot read {url}: {e}") from e
    finally:
        await gh.aclose()
    login = get_settings().github_login
    if not login or (pr.get("user") or {}).get("login", "").lower() != login.lower():
        raise RecordError(f"{url} is by {(pr.get('user') or {}).get('login')}, not you ({login or 'login unset'}).")
    canonical = pr["html_url"]
    async with session_scope() as s:
        repo = await get_or_create_repo(s, pr["base"]["repo"]["full_name"])
        for t in (await s.execute(select(Task).where(Task.repo_id == repo.id))).scalars():
            if (t.result or {}).get("pr_url") == canonical:
                raise RecordError(f"{canonical} is already in the track record.")
        task = Task(
            repo_id=repo.id, issue_number=number, status="published", base_commit=pr["base"]["sha"],
            issue={"title": pr["title"]}, plan={}, result={"pr_url": canonical, "imported": True},
            archived_at=datetime.now(UTC),
        )  # fmt: skip
        s.add(task)
        await s.flush()
    return task
