"""Read-only GitHub GraphQL client and the issue/PR fetcher.

Fetching is incremental: the first run takes every open item plus items closed in the
lookback window; later runs take only items updated since the last fetch.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from nightshift.config import get_settings

log = structlog.get_logger()

# Bump when the fetched fields change; repos stored by an older version are refetched in full once.
FETCH_VERSION = 3

BODY_LIMIT = 4000
# Closing lines of long bodies, where reporters often say they have a fix or will open a PR.
BODY_TAIL = 1000
COMMENT_LIMIT = 1500

_COMMENT_FIELDS = "databaseId author { login } authorAssociation body createdAt"
_COMMENTS = (
    f"comments(last: 5) {{ totalCount nodes {{ {_COMMENT_FIELDS} }} }} "
    f"firstComments: comments(first: 3) {{ nodes {{ {_COMMENT_FIELDS} }} }}"
)
_REF = "__typename ... on Issue { number } ... on PullRequest { number }"

ISSUES_QUERY = f"""
query($owner: String!, $name: String!, $cursor: String, $first: Int!, $states: [IssueState!], $since: DateTime) {{
  rateLimit {{ remaining resetAt }}
  repository(owner: $owner, name: $name) {{
    issues(first: $first, after: $cursor, states: $states, filterBy: {{since: $since}},
           orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        number title body url state stateReason createdAt updatedAt closedAt authorAssociation
        author {{ __typename login }}
        labels(first: 10) {{ nodes {{ name }} }}
        {_COMMENTS}
        timelineItems(last: 20, itemTypes: [CROSS_REFERENCED_EVENT, CLOSED_EVENT, MARKED_AS_DUPLICATE_EVENT]) {{
          nodes {{
            __typename
            ... on CrossReferencedEvent {{ isCrossRepository source {{ {_REF} }} }}
            ... on ClosedEvent {{ closer {{ __typename ... on PullRequest {{ number }} ... on Commit {{ oid }} }} }}
            ... on MarkedAsDuplicateEvent {{ canonical {{ {_REF} }} }}
          }}
        }}
      }}
    }}
  }}
}}
"""

PRS_QUERY = f"""
query($owner: String!, $name: String!, $cursor: String, $first: Int!, $states: [PullRequestState!]) {{
  rateLimit {{ remaining resetAt }}
  repository(owner: $owner, name: $name) {{
    pullRequests(first: $first, after: $cursor, states: $states, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        number title body url state createdAt updatedAt closedAt mergedAt isDraft authorAssociation
        mergeable reviewDecision additions deletions changedFiles
        author {{ __typename login }} mergedBy {{ login }}
        labels(first: 10) {{ nodes {{ name }} }}
        files(first: 50) {{ nodes {{ path }} }}
        closingIssuesReferences(first: 10) {{ nodes {{ number state stateReason }} }}
        commits(last: 1) {{ nodes {{ commit {{ committedDate statusCheckRollup {{ state }} }} }} }}
        reviews(last: 5) {{ nodes {{ state author {{ login }} authorAssociation submittedAt body }} }}
        firstReviews: reviews(first: 3) {{ nodes {{ author {{ login }} authorAssociation submittedAt }} }}
        {_COMMENTS}
        timelineItems(last: 20, itemTypes: [CROSS_REFERENCED_EVENT, MARKED_AS_DUPLICATE_EVENT]) {{
          nodes {{
            __typename
            ... on CrossReferencedEvent {{ isCrossRepository source {{ {_REF} }} }}
            ... on MarkedAsDuplicateEvent {{ canonical {{ {_REF} }} }}
          }}
        }}
      }}
    }}
  }}
}}
"""


# Documents that tell an outside contributor how the project wants to be worked on.
# Alias -> path at the default branch's HEAD; missing files come back as null.
REPO_DOCS = {
    "readme": "README.md",
    "contributing": "CONTRIBUTING.md",
    "contributing_github": ".github/CONTRIBUTING.md",
    "contributing_docs": "docs/CONTRIBUTING.md",
    "pr_template": ".github/PULL_REQUEST_TEMPLATE.md",
    "pr_template_lower": ".github/pull_request_template.md",
    "claude_md": "CLAUDE.md",
    "agents_md": "AGENTS.md",
}
_DOC_FIELDS = "\n".join(
    f'    {alias}: object(expression: "HEAD:{path}") {{ ... on Blob {{ text byteSize }} }}'
    for alias, path in REPO_DOCS.items()
)
REPO_DOCS_QUERY = f"""
query($owner: String!, $name: String!) {{
  rateLimit {{ remaining resetAt }}
  repository(owner: $owner, name: $name) {{
    description
    primaryLanguage {{ name }}
    defaultBranchRef {{ name target {{ oid }} }}
{_DOC_FIELDS}
  }}
}}
"""


class GitHubError(RuntimeError):
    pass


class GitHubTimeout(GitHubError):
    """GitHub gave up on the query (502/504), usually because the page was too heavy."""


PAGE_SIZE = {"issues": 50, "pullRequests": 25}
MIN_PAGE_SIZE = 5


class GitHubClient:
    def __init__(self, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        settings = get_settings()
        token = token if token is not None else settings.github_token.get_secret_value()
        if not token:
            raise GitHubError("GITHUB_TOKEN is not set. Create a fine-grained token with public read-only access.")
        self._url = settings.github_api_url
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "User-Agent": "nightshift"},
            timeout=60.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(5):
            resp = await self._http.post(self._url, json={"query": query, "variables": variables})
            if resp.status_code in (502, 504):
                raise GitHubTimeout(f"GitHub timed out on the query (HTTP {resp.status_code}).")
            if resp.status_code in (403, 429, 503):
                wait = float(resp.headers.get("retry-after", 2 ** (attempt + 2)))
                log.warning("github_backoff", status=resp.status_code, wait_s=wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 401:
                raise GitHubError("GitHub rejected GITHUB_TOKEN (401). Check that it is valid and not expired.")
            if resp.is_error:
                raise GitHubError(f"GitHub returned HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                payload = resp.json()
            except ValueError as e:  # GitHub sometimes cuts off a large response mid-body
                raise GitHubTimeout(f"GitHub returned an incomplete response ({e}).") from e
            if payload.get("errors"):
                raise GitHubError(payload["errors"][0].get("message", "GraphQL error"))
            data: dict[str, Any] = payload["data"]
            remaining = (data.get("rateLimit") or {}).get("remaining")
            if remaining is not None and remaining < 50:
                log.warning("github_rate_limit_low", remaining=remaining)
            return data
        raise GitHubError("GitHub kept rate limiting; giving up for now.")

    async def rest_get(self, path: str) -> Any:
        """GET a REST endpoint (path relative to the API root), with the same backoff as GraphQL."""
        url = self._url.removesuffix("/graphql") + path
        for attempt in range(5):
            resp = await self._http.get(url)
            if resp.status_code in (403, 429, 502, 503, 504):
                wait = float(resp.headers.get("retry-after", 2 ** (attempt + 2)))
                log.warning("github_backoff", status=resp.status_code, wait_s=wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 401:
                raise GitHubError("GitHub rejected GITHUB_TOKEN (401). Check that it is valid and not expired.")
            if resp.is_error:
                raise GitHubError(f"GitHub returned HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise GitHubError("GitHub kept rate limiting; giving up for now.")

    async def fetch_repo_docs(self, full_name: str) -> dict[str, Any]:
        """The repo's file tree at HEAD and its contribution documents. One GraphQL query plus one REST call."""
        owner, name = full_name.split("/", 1)
        data = await self.graphql(REPO_DOCS_QUERY, {"owner": owner, "name": name})
        repo = data.get("repository")
        if repo is None:
            raise GitHubError(f"Repository {full_name} not found.")
        branch = repo.get("defaultBranchRef") or {}
        oid = (branch.get("target") or {}).get("oid")
        docs = {alias: (repo.get(alias) or {}).get("text") for alias in REPO_DOCS}
        paths: list[str] = []
        truncated = False
        if oid:
            tree = await self.rest_get(f"/repos/{owner}/{name}/git/trees/{oid}?recursive=1")
            paths = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
            truncated = bool(tree.get("truncated"))
        return {
            "head_oid": oid,
            "default_branch": branch.get("name"),
            "description": repo.get("description"),
            "language": (repo.get("primaryLanguage") or {}).get("name"),
            "docs": {REPO_DOCS[a]: text for a, text in docs.items() if text},
            "paths": paths,
            "tree_truncated": truncated,
        }

    async def read_file(self, full_name: str, path: str) -> str | None:
        """Text of a file at HEAD, or None if it does not exist or is binary."""
        owner, name = full_name.split("/", 1)
        query = (
            "query($owner: String!, $name: String!, $expr: String!) { rateLimit { remaining } "
            "repository(owner: $owner, name: $name) { object(expression: $expr) { ... on Blob { text isBinary } } } }"
        )
        data = await self.graphql(query, {"owner": owner, "name": name, "expr": f"HEAD:{path}"})
        blob = (data.get("repository") or {}).get("object") or {}
        return None if blob.get("isBinary") else blob.get("text")

    async def search_code(self, full_name: str, query: str, limit: int = 20) -> list[str]:
        """Paths of files in the repo matching a code search query (GitHub's default-branch index)."""
        q = quote(f"{query} repo:{full_name}")
        result = await self.rest_get(f"/search/code?q={q}&per_page={limit}")
        return [item["path"] for item in result.get("items", [])]

    async def _pages(self, query: str, variables: dict[str, Any], conn: str) -> AsyncIterator[dict[str, Any]]:
        cursor = None
        size = PAGE_SIZE[conn]
        timeouts = 0
        while True:
            try:
                data = await self.graphql(query, {**variables, "cursor": cursor, "first": size})
            except GitHubTimeout:
                # Heavy pages time out on busy repos: retry the same cursor with a smaller page.
                timeouts += 1
                if timeouts > 6:
                    raise
                size = max(MIN_PAGE_SIZE, size // 2)
                log.warning("github_timeout_shrinking_page", conn=conn, page_size=size)
                await asyncio.sleep(2 * timeouts)
                continue
            repo = data.get("repository")
            if repo is None:
                raise GitHubError(f"Repository {variables['owner']}/{variables['name']} not found.")
            page = repo[conn]
            for node in page["nodes"]:
                yield node
            if not page["pageInfo"]["hasNextPage"]:
                return
            cursor = page["pageInfo"]["endCursor"]

    async def fetch_items(
        self, full_name: str, since: datetime | None, lookback_days: int, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return normalized issues and PRs, and whether `limit` cut the fetch short.

        `since=None` means a first, full run. Issues and PRs each get `limit`, so a repo
        with many issues still has its PRs fetched; open items come before closed ones.
        """
        owner, name = full_name.split("/", 1)
        base = {"owner": owner, "name": name}
        closed_since = since or datetime.now(UTC) - timedelta(days=lookback_days)
        out: list[dict[str, Any]] = []
        truncated = False

        issues = 0
        for states, after in [(["OPEN"], since), (["CLOSED"], closed_since)]:
            if issues >= limit:
                truncated = True
                break
            variables = {**base, "states": states, "since": after.isoformat() if after else None}
            async for node in self._pages(ISSUES_QUERY, variables, "issues"):
                out.append(normalize_issue(node))
                issues += 1
                if issues >= limit:
                    truncated = True
                    break

        prs = 0
        for states, after in [(["OPEN"], since), (["CLOSED", "MERGED"], closed_since)]:
            if prs >= limit:
                truncated = True
                break
            async for node in self._pages(PRS_QUERY, {**base, "states": states}, "pullRequests"):
                updated = _ts(node["updatedAt"])
                if after and updated and updated < after:
                    break  # ordered by updatedAt desc; the rest is older
                out.append(normalize_pr(node))
                prs += 1
                if prs >= limit:
                    truncated = True
                    break
        return out, truncated


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _login(node: dict[str, Any] | None) -> str | None:
    return (node or {}).get("login")


def _comments(node: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    c = node.get("comments") or {}
    items = [
        {
            "id": n["databaseId"],
            "author": _login(n.get("author")),
            "association": n.get("authorAssociation"),
            "body": (n.get("body") or "")[:COMMENT_LIMIT],
            "created_at": n["createdAt"],
        }
        for n in c.get("nodes", [])
    ]
    return items, c.get("totalCount", len(items))


def _first_responses(node: dict[str, Any]) -> list[dict[str, Any]]:
    """The earliest comments and reviews, used to measure how fast maintainers first respond."""
    out = [
        {"author": _login(n.get("author")), "association": n.get("authorAssociation"), "at": n.get("createdAt")}
        for n in (node.get("firstComments") or {}).get("nodes", [])
    ]
    out += [
        {"author": _login(n.get("author")), "association": n.get("authorAssociation"), "at": n.get("submittedAt")}
        for n in (node.get("firstReviews") or {}).get("nodes", [])
    ]
    return sorted((r for r in out if r["at"]), key=lambda r: r["at"])


def _timeline(node: dict[str, Any]) -> dict[str, Any]:
    refs: set[int] = set()
    duplicate_of = None
    closed_by = None
    for ev in (node.get("timelineItems") or {}).get("nodes", []):
        kind = ev.get("__typename")
        if kind == "CrossReferencedEvent" and not ev.get("isCrossRepository"):
            num = (ev.get("source") or {}).get("number")
            if num:
                refs.add(num)
        elif kind == "MarkedAsDuplicateEvent":
            duplicate_of = (ev.get("canonical") or {}).get("number") or duplicate_of
        elif kind == "ClosedEvent":
            closer = ev.get("closer") or {}
            if closer.get("__typename") == "PullRequest":
                closed_by = {"type": "pr", "number": closer["number"]}
            elif closer.get("__typename") == "Commit":
                closed_by = {"type": "commit", "oid": closer["oid"]}
    return {"cross_refs": sorted(refs), "duplicate_of": duplicate_of, "closed_by": closed_by}


def _common(node: dict[str, Any], kind: str) -> dict[str, Any]:
    comments, comment_count = _comments(node)
    body = node.get("body") or ""
    return {
        "number": node["number"],
        "kind": kind,
        "title": node["title"],
        "body": body[:BODY_LIMIT],
        "author": _login(node.get("author")),
        "url": node["url"],
        "labels": [lb["name"] for lb in (node.get("labels") or {}).get("nodes", [])],
        "created_at": _ts(node["createdAt"]),
        "updated_at": _ts(node["updatedAt"]),
        "closed_at": _ts(node.get("closedAt")),
        "data": {
            "comments": comments,
            "comment_count": comment_count,
            "author_association": node.get("authorAssociation"),
            "author_type": (node.get("author") or {}).get("__typename"),  # "Bot" for apps like github-actions
            "first_responses": _first_responses(node),
            "body_tail": body[-BODY_TAIL:] if len(body) > BODY_LIMIT else "",
            **_timeline(node),
        },
    }


def normalize_issue(node: dict[str, Any]) -> dict[str, Any]:
    item = _common(node, "issue")
    item["state"] = node["state"]
    item["state_reason"] = node.get("stateReason")
    return item


def normalize_pr(node: dict[str, Any]) -> dict[str, Any]:
    item = _common(node, "pr")
    item["state"] = node["state"]  # OPEN | CLOSED | MERGED
    item["state_reason"] = None
    last_commit = ((node.get("commits") or {}).get("nodes") or [{}])[-1].get("commit") or {}
    item["data"].update(
        {
            "is_draft": node.get("isDraft", False),
            "mergeable": node.get("mergeable"),
            "review_decision": node.get("reviewDecision"),
            "additions": node.get("additions", 0),
            "deletions": node.get("deletions", 0),
            "changed_files": node.get("changedFiles", 0),
            "files": [f["path"] for f in (node.get("files") or {}).get("nodes", [])],
            "closing_issues": [
                {"number": i["number"], "state": i["state"], "state_reason": i.get("stateReason")}
                for i in (node.get("closingIssuesReferences") or {}).get("nodes", [])
            ],
            "ci_state": (last_commit.get("statusCheckRollup") or {}).get("state"),
            "last_commit_at": last_commit.get("committedDate"),
            "merged_at": node.get("mergedAt"),
            "merged_by": _login(node.get("mergedBy")),
            "reviews": [
                {
                    "state": r["state"],
                    "author": _login(r.get("author")),
                    "association": r.get("authorAssociation"),
                    "submitted_at": r.get("submittedAt"),
                    "body": (r.get("body") or "")[:COMMENT_LIMIT],
                }
                for r in (node.get("reviews") or {}).get("nodes", [])
            ],
        }
    )
    return item
