"""The issue a task works on, fetched fresh from GitHub, with the facts that say whether it is still free.

Whether someone already has it is decided by code, from five sources, because each misses some (see
_linked_prs). A PR found for sure (it closes the issue, names it, or a comment says it fixes it) blocks
planning; one found only by shared terms is a possible match, shown as a warning at Gate 1.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from nightshift.github import GitHubClient, GitHubError

COMMENTS = 30
BODY_CHARS = 12_000
NAMED_LIMIT = 6  # PRs named in comments that are looked up, per issue
DUPLICATES_READ = 3  # closed, unmerged referencing PRs whose comments are read
SIMILAR_LIMIT = 5

# A comment naming a PR in these words says that PR does the issue's work ("This fixes the same problem as #4455").
FIX_WORDS = re.compile(
    r"\b(fix(es|ed)?|same (problem|issue|change|fix)|duplicates?|covered (by|in)|covers|addressed (by|in)"
    r"|resolved (by|in)|superseded|already (in review|merged|fixed|open)|implemented (by|in))\b",
    re.I,
)
PR_REF = re.compile(r"(?:#|/pull/)(\d+)\b")
# Words too common in titles, or too generic as code names, to find the same change by
STOPWORDS = frozenset(
    re.findall(
        r"\S+",
        """a an and are as at be but by can cannot does doesn't don't for from has have in into is isn't it its not of
        on or should that the this to when which while with without error errors issue bug fix fails failing failed
        support add allow use using after before only since then there work works working
        key value self args kwargs none true false data obj""",
    )
)


class LinkedPR(BaseModel):
    number: int
    author: str | None
    state: str  # open | closed | merged
    title: str
    how: str = ""  # how it was found, said in a refusal or a warning
    likely: bool = True  # False: found only by shared terms, so a possible match, not a blocker


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
            if pr.likely and pr.state in ("open", "merged") and pr.author != operator:
                out.append(f"{pr.state} PR #{pr.number} by {pr.author} {pr.how or 'references it'}")
        return out

    def possible(self, operator: str | None) -> list[str]:
        """Open or merged PRs that may do the same work, found only by shared terms: worth a look before approving."""
        return [
            f'{pr.state} PR #{pr.number} by {pr.author} may already do this: "{pr.title}" ({pr.how})'
            for pr in self.linked_prs
            if not pr.likely and pr.state in ("open", "merged") and pr.author != operator
        ]


async def fetch_issue(full_name: str, number: int) -> IssueView:
    gh = GitHubClient()
    try:
        raw = await gh.rest_get(f"/repos/{full_name}/issues/{number}")
        if "pull_request" in raw:
            raise GitHubError(f"#{number} is a pull request, not an issue.")
        comments = await gh.rest_get(f"/repos/{full_name}/issues/{number}/comments?per_page={COMMENTS}")
        linked = await _linked_prs(gh, full_name, number, raw, comments)
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


async def _linked_prs(
    gh: GitHubClient, full_name: str, number: int, issue: dict[str, Any], comments: list[dict[str, Any]]
) -> list[LinkedPR]:
    """Pull requests that take on the issue, from five sources, because each misses some:
    1. PRs that will close it (GraphQL);
    2. timeline cross-references (hidden from some tokens);
    3. open PRs whose title or body names it (search, checked with an exact `#N` match);
    4. PRs named in a comment, on the issue or on a closed referencing PR, as doing its work: a fix PR that never
       names the issue is often only known this way (redis-py #4454 was fixed by #4455; a maintainer said so on
       the duplicate #4462);
    5. PRs opened since the issue whose titles share its key terms: possible matches only."""
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
            how="will close it",
        )

    timeline = await gh.rest_get(f"/repos/{full_name}/issues/{number}/timeline?per_page=100")
    for ev in timeline if isinstance(timeline, list) else []:
        src = (ev.get("source") or {}).get("issue") or {}
        if ev.get("event") == "cross-referenced" and src.get("pull_request"):
            state = "merged" if src["pull_request"].get("merged_at") else src.get("state", "open")
            found.setdefault(
                src["number"],
                LinkedPR(number=src["number"], author=(src.get("user") or {}).get("login"), state=state,
                         title=src.get("title", ""), how="references it"),
            )  # fmt: skip

    mention = re.compile(rf"#{number}\b")
    hits = await gh.rest_get(f"/search/issues?q={quote(f'repo:{full_name} is:pr is:open {number}')}&per_page=50")
    for item in hits.get("items", []):
        if mention.search(f"{item.get('title', '')}\n{item.get('body') or ''}"):
            found.setdefault(
                item["number"],
                LinkedPR(number=item["number"], author=(item.get("user") or {}).get("login"), state="open",
                         title=item["title"], how="names it"),
            )  # fmt: skip

    await _named_in_comments(gh, full_name, number, comments, found)
    await _similar(gh, full_name, number, issue, found)
    return sorted(found.values(), key=lambda p: p.number)


async def _named_in_comments(
    gh: GitHubClient, full_name: str, number: int, comments: list[dict[str, Any]], found: dict[int, LinkedPR]
) -> None:
    """Source 4: PRs named in the issue's comments, and in the comments of referencing PRs closed unmerged."""
    said: list[tuple[str, str]] = [(c.get("body") or "", "a comment on the issue") for c in comments]
    for pr in [p for p in found.values() if p.state == "closed"][:DUPLICATES_READ]:
        try:
            for c in await gh.rest_get(f"/repos/{full_name}/issues/{pr.number}/comments?per_page={COMMENTS}"):
                said.append((c.get("body") or "", f"a comment on #{pr.number}"))
        except GitHubError:
            continue
    named: dict[int, tuple[bool, str]] = {}
    for body, where in said:
        for ref in PR_REF.findall(body):
            n = int(ref)
            if n == number or n in found:
                continue
            fixes = bool(FIX_WORDS.search(body))
            if n not in named or (fixes and not named[n][0]):
                named[n] = (fixes, where)
    for n, (fixes, where) in list(named.items())[:NAMED_LIMIT]:
        try:
            pr = await gh.rest_get(f"/repos/{full_name}/pulls/{n}")
        except GitHubError:  # an issue, or gone
            continue
        state = "merged" if pr.get("merged_at") else pr.get("state", "open")
        found[n] = LinkedPR(
            number=n, author=(pr.get("user") or {}).get("login"), state=state, title=pr.get("title", ""),
            how=f"does its work, says {where}" if fixes else f"is named in {where}", likely=fixes,
        )  # fmt: skip


def key_terms(title: str, limit: int = 2) -> list[str]:
    """The title's most distinctive words, for a search that needs all of them: code-like ones first (from
    `backticks`, calls, dotted or snake_case names), longest first, then the longest other words.
    `ts().alter(key, labels={}) does not remove the labels` -> labels, alter."""
    code_parts = re.findall(r"`([^`]+)`", title) + [w for w in title.split() if re.search(r"[()._{}\[\]=:/]", w)]
    code = [w for part in code_parts for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", part)]
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{4,}", title)
    terms: list[str] = []
    for w in sorted(code, key=len, reverse=True) + sorted(words, key=len, reverse=True):
        w = w.lower()
        if len(terms) < limit and len(w) >= 3 and w not in STOPWORDS and w not in terms:
            terms.append(w)
    return terms


async def _similar(
    gh: GitHubClient, full_name: str, number: int, issue: dict[str, Any], found: dict[int, LinkedPR]
) -> None:
    """Source 5: PRs opened since the issue whose titles share its key terms. Shared words prove nothing,
    so these are possible matches, shown as a warning; a failed search is skipped."""
    terms = key_terms(issue.get("title", ""))
    if len(terms) < 2:
        return
    since = (issue.get("created_at") or "")[:10]
    q = f"repo:{full_name} is:pr in:title {' '.join(terms)}" + (f" created:>={since}" if since else "")
    try:
        hits = await gh.rest_get(f"/search/issues?q={quote(q)}&per_page={SIMILAR_LIMIT}")
    except GitHubError:
        return
    for item in hits.get("items", [])[:SIMILAR_LIMIT]:
        n = item["number"]
        if n == number or n in found:
            continue
        pr = item.get("pull_request") or {}
        state = "merged" if pr.get("merged_at") else item.get("state", "open")
        found[n] = LinkedPR(
            number=n, author=(item.get("user") or {}).get("login"), state=state, title=item.get("title", ""),
            how=f"its title shares {', '.join(terms)}", likely=False,
        )  # fmt: skip
