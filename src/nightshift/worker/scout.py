"""Scout repos you do not watch yet: should you add them? Public GitHub data only, no model, no setup.

For each candidate: how its recent outside PRs fared (the same pulse the contributor report uses), how well
it fits you (worker/fit.py), open starter issues, whether the contributing guide asks for a CLA or says
anything about AI-written changes, and its size, a rough guide to what a setup costs. Kept for a few hours.

Org members who keep their membership private show up as outside contributors, so a big company repo's
merge rate reads somewhat higher than what a stranger gets; people who merged PRs count as maintainers.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from nightshift.db import Item
from nightshift.github import GitHubClient, GitHubError
from nightshift.health.contributor import pulse
from nightshift.worker.fit import fit

log = structlog.get_logger()

WINDOW_DAYS = 90
PRS = 100  # PRs per page, newest first
PAGES = 4  # at most this many pages per repo: a busy repo opens hundreds of PRs a month
AT_ONCE = 3
CACHE_SECONDS = 6 * 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# A starting list: popular repos from big companies and trending AI projects, mostly Python, Go and TypeScript.
SUGGESTED = [
    "google/adk-python", "googleapis/python-genai", "googleapis/mcp-toolbox", "google/osv-scanner",
    "microsoft/autogen", "microsoft/markitdown", "microsoft/playwright-python", "microsoft/typescript-go",
    "microsoft/semantic-kernel", "awslabs/mcp", "aws-powertools/powertools-lambda-python", "aws/aws-sdk-pandas",
    "pydantic/pydantic-ai", "openai/openai-agents-python", "huggingface/smolagents", "langchain-ai/langgraph",
    "fastapi/fastapi", "anthropics/anthropic-sdk-python",
]  # fmt: skip

CLA = re.compile(r"\bCLA\b|Contributor License Agreement|cla\.developers\.google|cla\.opensource\.microsoft", re.I)
# Repos synced from an internal tree (Google's Copybara) close outside PRs after landing them; a bot says so.
LANDED = re.compile(
    r"imported and merged|merged via copybara|landed (?:in|as|internally)|has been (?:merged|submitted|imported)"
    r"|merged (?:it )?internally|changes are now in the main branch",
    re.I,
)
AI_LINE = re.compile(r"\b(AI|LLM|Copilot|ChatGPT|Claude|generative|machine[- ]generated|AI[- ]generated)\b")

PR_FIELDS = """number state createdAt mergedAt closedAt authorAssociation author { __typename login } mergedBy { login }
    firstComments: comments(first: 5) { nodes { author { login } authorAssociation createdAt } }
    firstReviews: reviews(first: 5) { nodes { author { login } authorAssociation submittedAt } }
    lastComments: comments(last: 2) { nodes { body } }"""
PAGE = "pageInfo { hasPreviousPage startCursor }"

QUERY = f"""query($owner: String!, $name: String!, $prs: Int!) {{ repository(owner: $owner, name: $name) {{
  nameWithOwner description stargazerCount diskUsage isArchived pushedAt primaryLanguage {{ name }}
  good: issues(states: OPEN, labels: ["good first issue", "good-first-issue", "Good First Issue"]) {{ totalCount }}
  help: issues(states: OPEN, labels: ["help wanted", "help-wanted", "Help Wanted"]) {{ totalCount }}
  c1: object(expression: "HEAD:CONTRIBUTING.md") {{ ... on Blob {{ text }} }}
  c2: object(expression: "HEAD:.github/CONTRIBUTING.md") {{ ... on Blob {{ text }} }}
  c3: object(expression: "HEAD:docs/CONTRIBUTING.md") {{ ... on Blob {{ text }} }}
  pullRequests(last: $prs, orderBy: {{field: CREATED_AT, direction: ASC}}) {{ {PAGE} nodes {{ {PR_FIELDS} }} }} }} }}"""
MORE = f"""query($owner: String!, $name: String!, $prs: Int!, $before: String!) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(last: $prs, before: $before, orderBy: {{field: CREATED_AT, direction: ASC}}) {{
      {PAGE} nodes {{ {PR_FIELDS} }} }} }} }}"""


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _items(nodes: list[dict[str, Any]]) -> list[Item]:
    """PR nodes as the transient Items `pulse` reads."""
    out = []
    for n in nodes:
        responses = [
            {"author": (c.get("author") or {}).get("login"), "association": c.get("authorAssociation"), "at": at}
            for key, when in (("firstComments", "createdAt"), ("firstReviews", "submittedAt"))
            for c in (n.get(key) or {}).get("nodes") or []
            if (at := c.get(when))
        ]
        # Landed from an internal tree and closed by a bot: merged in all but name.
        landed = n["state"] == "CLOSED" and any(
            LANDED.search(c.get("body") or "") for c in (n.get("lastComments") or {}).get("nodes") or []
        )
        out.append(
            Item(
                kind="pr",
                number=n["number"],
                state="MERGED" if landed else n["state"],
                author=(n.get("author") or {}).get("login"),
                created_at=_ts(n["createdAt"]),
                data={
                    "author_association": n.get("authorAssociation"),
                    "author_type": (n.get("author") or {}).get("__typename"),
                    "merged_by": (n.get("mergedBy") or {}).get("login"),
                    "merged_at": n.get("closedAt") if landed else n.get("mergedAt"),
                    "first_responses": sorted(responses, key=lambda r: r["at"]),
                    "comments": [],
                },
            )  # fmt: skip
        )
    return out


def guide_notes(text: str) -> dict[str, Any]:
    """What the contributing guide says about a CLA and about AI-written changes."""
    lines = [ln.strip(" -*#>\t") for ln in text.splitlines()]
    ai = [ln for ln in lines if AI_LINE.search(ln) and 20 <= len(ln) <= 400]
    return {"cla": bool(CLA.search(text)), "ai_policy": ai[:2]}


def row(repo: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """One candidate's facts from the GraphQL answer."""
    guide = next((o["text"] for k in ("c1", "c2", "c3") if (o := repo.get(k)) and o.get("text")), "")
    nodes = (repo.get("pullRequests") or {}).get("nodes") or []
    p = pulse(_items(nodes), WINDOW_DAYS, now)
    oldest = min((t for n in nodes if (t := _ts(n["createdAt"]))), default=None)
    covers = min(WINDOW_DAYS, ((now or datetime.now(UTC)) - oldest).days) if oldest else 0
    return {
        "repo": repo["nameWithOwner"],
        "description": repo.get("description") or "",
        "stars": repo.get("stargazerCount", 0),
        "size_mb": round((repo.get("diskUsage") or 0) / 1024),
        "language": (repo.get("primaryLanguage") or {}).get("name"),
        "archived": bool(repo.get("isArchived")),
        "pushed_at": repo.get("pushedAt"),
        "good_first": (repo.get("good") or {}).get("totalCount", 0),
        "help_wanted": (repo.get("help") or {}).get("totalCount", 0),
        "guide": bool(guide),
        **guide_notes(guide),
        "pulse": p.to_dict(),
        "covers_days": covers,  # a busy repo's newest PRs may span less than the window
    }


def rank(r: dict[str, Any]) -> float:
    """Order for the list: outside PRs that get merged, then fit; inactive, archived and hard repos sink."""
    p = r.get("pulse") or {}
    if r.get("archived") or p.get("verdict") == "inactive":
        return -1.0
    if p.get("merge_rate") is None:  # no outside PRs decided in the window: nothing to go on but fit
        return round(0.2 * ((r.get("fit") or {}).get("score") or 0.0), 3)
    value = 0.6 * float(p["merge_rate"]) + 0.4 * ((r.get("fit") or {}).get("score") or 0.0)
    return round(value * (0.5 if p.get("verdict") == "hard" else 1.0), 3)


async def _one(gh: GitHubClient, name: str) -> dict[str, Any]:
    if (hit := _cache.get(name.lower())) and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    owner, repo = name.split("/", 1)
    try:
        data = await gh.graphql(QUERY, {"owner": owner, "name": repo, "prs": PRS})
    except GitHubError as e:
        return {"repo": name, "error": f"GitHub did not answer: {e}"}
    if not (repo_data := data.get("repository")):
        return {"repo": name, "error": "No such public repository."}
    prs = repo_data["pullRequests"]
    since = datetime.now(UTC).timestamp() - WINDOW_DAYS * 86400
    for _ in range(PAGES - 1):  # older pages until the window is covered
        nodes, page = prs["nodes"], prs["pageInfo"]
        oldest = _ts(nodes[0]["createdAt"]) if nodes else None
        if not page["hasPreviousPage"] or oldest is None or oldest.timestamp() < since:
            break
        try:
            more = await gh.graphql(MORE, {"owner": owner, "name": repo, "prs": PRS, "before": page["startCursor"]})
        except GitHubError as e:  # what was read still counts; the coverage says how far back
            log.warning("scout_page_failed", repo=name, error=str(e))
            break
        older = more["repository"]["pullRequests"]
        prs = {"nodes": older["nodes"] + nodes, "pageInfo": older["pageInfo"]}
    repo_data["pullRequests"] = prs
    out = row(repo_data)
    _cache[name.lower()] = (time.monotonic(), out)
    return out


async def scout(repos: list[str], watched: set[str] | None = None) -> dict[str, Any]:
    """{"rows": [...] best first, "fit_error": str | None}. `watched`: names already on the watchlist."""
    names = list(dict.fromkeys(r.strip().strip("/") for r in repos if r.strip().count("/") == 1))
    gate = asyncio.Semaphore(AT_ONCE)
    gh = GitHubClient()

    async def one(name: str) -> dict[str, Any]:
        async with gate:
            return await _one(gh, name)

    try:
        rows = await asyncio.gather(*(one(n) for n in names))
    finally:
        await gh.aclose()
    fits = await fit([r["repo"] for r in rows if "error" not in r])
    known = {w.lower() for w in watched or set()}
    for r in rows:
        r["fit"] = (fits.get("repos") or {}).get(r["repo"])
        r["watched"] = r["repo"].lower() in known
        r["rank"] = rank(r) if "error" not in r else -2.0
    rows.sort(key=lambda r: -r["rank"])
    return {"rows": rows, "fit_error": fits.get("error"), "window_days": WINDOW_DAYS,
            "checked_at": datetime.now(UTC).isoformat()}  # fmt: skip
