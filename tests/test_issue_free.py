"""Whether an issue is still free, including fixes that never name it: redis-py #4454 was fixed by #4455, which
did not mention it; only a maintainer's comment on the duplicate #4462 said so. GitHub is faked."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

import pytest

from nightshift.github import GitHubError
from nightshift.worker import issue as issue_mod
from nightshift.worker.issue import fetch_issue, key_terms

REPO = "redis/redis-py"
TITLE = "ts().alter(key, labels={}) does not remove the labels of a time series"


def _pr(n: int, title: str, by: str, merged: bool = False, state: str = "closed") -> dict[str, Any]:
    return {"number": n, "title": title, "user": {"login": by}, "state": state,
            "merged_at": "2026-10-22T10:00:00Z" if merged else None}  # fmt: skip


class FakeGitHub:
    def __init__(self, routes: dict[str, Any], searches: list[str]) -> None:
        self.routes, self.searches = routes, searches

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": []}}}}

    async def rest_get(self, path: str) -> Any:
        if path.startswith("/search/issues"):
            q = unquote(path.split("q=", 1)[1].split("&", 1)[0])
            self.searches.append(q)
            return self.routes.get("similar", {"items": []}) if "in:title" in q else {"items": []}
        key = path.split("?", 1)[0]
        if key not in self.routes:
            raise GitHubError(f"404 {key}")
        return self.routes[key]

    async def aclose(self) -> None:
        pass


def _routes(**extra: Any) -> dict[str, Any]:
    base = f"/repos/{REPO}"
    routes = {
        f"{base}/issues/4454": {"title": TITLE, "body": "", "state": "open", "user": {"login": "RaphaelFakhri"},
                                "labels": [], "assignees": [], "created_at": "2026-10-18T09:00:00Z",
                                "html_url": f"https://github.com/{REPO}/issues/4454"},
        f"{base}/issues/4454/comments": [{"user": {"login": "petyaslavova"}, "created_at": "2026-10-19T13:02:29Z",
                                          "body": "Hey @RaphaelFakhri, thanks for reporting this!"}],
        f"{base}/issues/4454/timeline": [
            {"event": "cross-referenced", "source": {"issue": {**_pr(4462, "Fix TS.ALTER labels={}", "ekanshul"),
                                                              "pull_request": {"merged_at": None}}}},
        ],
        f"{base}/issues/4462/comments": [{"user": {"login": "petyaslavova"}, "body":
                                          "This fixes the same problem as #4455, which was opened earlier by the "
                                          "reporter of #4454 and is already in review."}],
        f"{base}/pulls/4455": _pr(4455, "Send LABELS from TS.ALTER when labels is an empty dict", "RaphaelFakhri",
                                  merged=True),
    }  # fmt: skip
    routes.update(extra)
    return routes


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Any:
    def use(routes: dict[str, Any]) -> list[str]:
        searches: list[str] = []
        monkeypatch.setattr(issue_mod, "GitHubClient", lambda: FakeGitHub(routes, searches))
        return searches

    return use


async def test_a_fix_named_only_on_a_duplicate_pr_blocks_the_issue(github: Any) -> None:
    github(_routes())
    view = await fetch_issue(REPO, 4454)
    blockers = view.blockers("someone-else")
    assert blockers == ["merged PR #4455 by RaphaelFakhri does its work, says a comment on #4462"]


async def test_a_pr_named_without_fix_words_is_only_a_possible_match(github: Any) -> None:
    routes = _routes()
    routes[f"/repos/{REPO}/issues/4462/comments"] = [{"user": {"login": "x"}, "body": "Related reading: #4455"}]
    github(routes)
    view = await fetch_issue(REPO, 4454)
    assert view.blockers("me") == []
    assert view.possible("me") == [
        'merged PR #4455 by RaphaelFakhri may already do this: "Send LABELS from TS.ALTER when labels is an empty '
        'dict" (is named in a comment on #4462)'
    ]


async def test_a_pr_with_the_same_key_terms_is_a_possible_match(github: Any) -> None:
    routes = _routes()
    routes[f"/repos/{REPO}/issues/4462/comments"] = []
    routes["similar"] = {"items": [{**_pr(9001, "Fix empty labels in TS.ALTER", "dev", state="open"),
                                    "pull_request": {}}]}  # fmt: skip
    searches = github(routes)
    view = await fetch_issue(REPO, 4454)
    assert view.blockers("me") == []
    assert view.possible("me") == [
        'open PR #9001 by dev may already do this: "Fix empty labels in TS.ALTER" (its title shares labels, alter)'
    ]
    assert f"repo:{REPO} is:pr in:title labels alter created:>=2026-10-18" in searches


async def test_a_named_number_that_is_not_a_pr_is_ignored(github: Any) -> None:
    routes = _routes()
    del routes[f"/repos/{REPO}/pulls/4455"]  # #4455 is an issue: /pulls/4455 is a 404
    github(routes)
    view = await fetch_issue(REPO, 4454)
    assert view.blockers("me") == [] and view.possible("me") == []


async def test_your_own_pr_never_blocks_you(github: Any) -> None:
    github(_routes())
    view = await fetch_issue(REPO, 4454)
    assert view.blockers("RaphaelFakhri") == []


def test_key_terms_prefer_code_names() -> None:
    assert key_terms(TITLE) == ["labels", "alter"]
    assert key_terms("`start_server` context manager leaks") == ["start_server", "context"]
    assert key_terms("Crash") == ["crash"]  # one term: too few to search by (_similar skips it)
