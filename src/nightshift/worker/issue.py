"""The issue a task works on, fetched fresh from GitHub, with the facts that say whether it is still free."""

from __future__ import annotations

import re
from urllib.parse import quote

from pydantic import BaseModel

from nightshift.github import GitHubClient, GitHubError

COMMENTS = 30
BODY_CHARS = 12_000


class LinkedPR(BaseModel):
    number: int
    author: str | None
    state: str  # open | closed | merged
    title: str


class IssueView(BaseModel):
    number: int
    title: str
    body: str
    state: str
    author: str | None
    labels: list[str]
    assignees: list[str]
    comments: list[dict[str, str]]  # {author, created_at, body}
    linked_prs: list[LinkedPR]
    url: str

    def blockers(self, operator: str | None) -> list[str]:
        """Facts that mean someone else already has this issue. Code decides these, not a model."""
        out = []
        if self.state != "open":
            out.append(f"issue is {self.state}")
        others = [a for a in self.assignees if a != operator]
        if others:
            out.append("assigned to " + ", ".join(others))
        for pr in self.linked_prs:
            if pr.state in ("open", "merged") and pr.author != operator:
                out.append(f"{pr.state} PR #{pr.number} by {pr.author} references it")
        return out


async def fetch_issue(full_name: str, number: int) -> IssueView:
    gh = GitHubClient()
    try:
        raw = await gh.rest_get(f"/repos/{full_name}/issues/{number}")
        if "pull_request" in raw:
            raise GitHubError(f"#{number} is a pull request, not an issue.")
        comments = await gh.rest_get(f"/repos/{full_name}/issues/{number}/comments?per_page={COMMENTS}")
        linked = await _linked_prs(gh, full_name, number)
    finally:
        await gh.aclose()
    return IssueView(
        number=number,
        title=raw["title"],
        body=(raw.get("body") or "")[:BODY_CHARS],
        state=raw["state"],
        author=(raw.get("user") or {}).get("login"),
        labels=[lb["name"] for lb in raw.get("labels", [])],
        assignees=[a["login"] for a in raw.get("assignees", [])],
        comments=[
            {"author": (c.get("user") or {}).get("login") or "", "created_at": c["created_at"], "body": c["body"] or ""}
            for c in comments
        ],
        linked_prs=linked,
        url=raw["html_url"],
    )


CLOSING_PRS = """query($owner: String!, $name: String!, $number: Int!) { rateLimit { remaining }
  repository(owner: $owner, name: $name) { issue(number: $number) {
    closedByPullRequestsReferences(first: 20, includeClosedPrs: true) {
      nodes { number state title author { login } } } } } }"""


async def _linked_prs(gh: GitHubClient, full_name: str, number: int) -> list[LinkedPR]:
    """Pull requests that take on the issue, from three sources, because each misses some:
    PRs that will close it (GraphQL), timeline cross-references (hidden from some tokens), and open PRs
    whose title or body names it (search, checked with an exact `#N` match)."""
    owner, name = full_name.split("/", 1)
    found: dict[int, LinkedPR] = {}

    data = await gh.graphql(CLOSING_PRS, {"owner": owner, "name": name, "number": number})
    for pr in (((data.get("repository") or {}).get("issue") or {}).get("closedByPullRequestsReferences") or {}).get(
        "nodes", []
    ):
        found[pr["number"]] = LinkedPR(
            number=pr["number"],
            author=(pr.get("author") or {}).get("login"),
            state=pr["state"].lower(),
            title=pr["title"],
        )

    for ev in await gh.rest_get(f"/repos/{full_name}/issues/{number}/timeline?per_page=100"):
        src = (ev.get("source") or {}).get("issue") or {}
        if ev.get("event") == "cross-referenced" and src.get("pull_request"):
            state = "merged" if src["pull_request"].get("merged_at") else src.get("state", "open")
            found.setdefault(
                src["number"],
                LinkedPR(number=src["number"], author=(src.get("user") or {}).get("login"), state=state,
                         title=src.get("title", "")),
            )  # fmt: skip

    mention = re.compile(rf"#{number}\b")
    hits = await gh.rest_get(f"/search/issues?q={quote(f'repo:{full_name} is:pr is:open {number}')}&per_page=50")
    for item in hits.get("items", []):
        if mention.search(f"{item.get('title', '')}\n{item.get('body') or ''}"):
            found.setdefault(
                item["number"],
                LinkedPR(number=item["number"], author=(item.get("user") or {}).get("login"), state="open",
                         title=item["title"]),
            )  # fmt: skip
    return sorted(found.values(), key=lambda p: p.number)
