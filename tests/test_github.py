from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nightshift import github
from nightshift.config import get_settings
from nightshift.github import GitHubClient, GitHubError, normalize_issue, normalize_pr

ISSUE_NODE = {
    "number": 5,
    "title": "Crash on Windows paths",
    "body": "x" * 10_000,
    "url": "https://github.com/o/r/issues/5",
    "state": "CLOSED",
    "stateReason": "COMPLETED",
    "createdAt": "2026-09-01T00:00:00Z",
    "updatedAt": "2026-09-05T00:00:00Z",
    "closedAt": "2026-09-05T00:00:00Z",
    "author": {"login": "bob"},
    "labels": {"nodes": [{"name": "bug"}]},
    "comments": {
        "totalCount": 1,
        "nodes": [{"databaseId": 1, "author": None, "body": "same", "createdAt": "2026-09-02T00:00:00Z"}],
    },
    "timelineItems": {
        "nodes": [
            {
                "__typename": "CrossReferencedEvent",
                "isCrossRepository": False,
                "source": {"__typename": "PullRequest", "number": 9},
            },
            {
                "__typename": "CrossReferencedEvent",
                "isCrossRepository": True,
                "source": {"__typename": "Issue", "number": 77},
            },
            {"__typename": "ClosedEvent", "closer": {"__typename": "PullRequest", "number": 9}},
        ]
    },
}


def test_normalize_issue() -> None:
    item = normalize_issue(ISSUE_NODE)
    assert item["kind"] == "issue"
    assert len(item["body"]) == 4000
    assert item["labels"] == ["bug"]
    assert item["data"]["cross_refs"] == [9]  # cross-repo refs ignored
    assert item["data"]["closed_by"] == {"type": "pr", "number": 9}
    assert item["data"]["comments"][0]["author"] is None


def test_normalize_pr() -> None:
    node = {
        **ISSUE_NODE,
        "number": 9,
        "state": "OPEN",
        "mergedAt": None,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "additions": 3,
        "deletions": 1,
        "changedFiles": 1,
        "files": {"nodes": [{"path": "a.py"}]},
        "closingIssuesReferences": {"nodes": [{"number": 5, "state": "OPEN", "stateReason": None}]},
        "commits": {
            "nodes": [{"commit": {"committedDate": "2026-09-03T00:00:00Z", "statusCheckRollup": {"state": "SUCCESS"}}}]
        },
        "reviews": {"nodes": []},
    }
    item = normalize_pr(node)
    assert item["data"]["ci_state"] == "SUCCESS"
    assert item["data"]["files"] == ["a.py"]
    assert item["data"]["closing_issues"][0]["number"] == 5


async def test_graphql_errors_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Could not resolve to a Repository"}]})

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubError, match="Could not resolve"):
        await client.graphql("query { x }", {})
    await client.aclose()


def test_missing_token_is_a_clear_error() -> None:
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        GitHubClient(token="")


async def test_issue_cap_does_not_starve_prs() -> None:
    """A repo with more open issues than the cap must still have its PRs fetched."""
    pr_node = {
        **ISSUE_NODE,
        "number": 9,
        "state": "OPEN",
        "updatedAt": "2099-01-01T00:00:00Z",
        "mergedAt": None,
        "closingIssuesReferences": {"nodes": []},
        "commits": {"nodes": []},
        "reviews": {"nodes": []},
    }
    seen_urls: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.add(str(request.url))
        body = request.read().decode()
        conn, node = ("pullRequests", pr_node) if "pullRequests(" in body else ("issues", ISSUE_NODE)
        page = {"pageInfo": {"hasNextPage": True, "endCursor": "c"}, "nodes": [node, node]}
        return httpx.Response(200, json={"data": {"repository": {conn: page}}})

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    items, truncated, gaps = await client.fetch_items("o/r", since=None, lookback_days=90, limit=3)
    await client.aclose()
    kinds = [i["kind"] for i in items]
    assert kinds.count("issue") == 3 and kinds.count("pr") == 3
    assert truncated and gaps == []
    assert seen_urls == {"https://api.github.com/graphql"}  # no trailing slash


async def test_timeout_retries_with_smaller_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nightshift.github.asyncio.sleep", _no_sleep)
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        first = json.loads(request.read())["variables"]["first"]
        sizes.append(first)
        if first > 10:
            return httpx.Response(504, text="<html>timeout</html>")
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [ISSUE_NODE]}
        return httpx.Response(200, json={"data": {"repository": {"issues": page}}})

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    nodes = [n async for n in client._pages("q", {"owner": "o", "name": "r"}, "issues")]
    await client.aclose()
    assert len(nodes) == 1
    assert sizes == [50, 25, 12, 6]


async def _no_sleep(_: float) -> None:
    return None


async def test_truncated_json_is_retried_with_smaller_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nightshift.github.asyncio.sleep", _no_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text='{"data": {"repository": {"issues": {"nodes": [{"title": "cut')
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [ISSUE_NODE]}
        return httpx.Response(200, json={"data": {"repository": {"issues": page}}})

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    nodes = [n async for n in client._pages("q", {"owner": "o", "name": "r"}, "issues")]
    await client.aclose()
    assert len(nodes) == 1 and calls == 2


def test_long_body_keeps_its_tail() -> None:
    item = normalize_issue({**ISSUE_NODE, "body": "a" * 5000 + "Happy to open a PR."})
    assert len(item["body"]) == 4000
    assert item["data"]["body_tail"].endswith("Happy to open a PR.")
    assert normalize_issue({**ISSUE_NODE, "body": "short"})["data"]["body_tail"] == ""


def _page(conn: str, nodes: list[dict[str, Any]]) -> httpx.Response:
    page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
    return httpx.Response(200, json={"data": {"repository": {conn: page}}})


async def test_null_bytes_are_dropped_from_everything_github_returns() -> None:
    """jaegertracing/jaeger: a PR body with a null byte, which Postgres cannot store, failed a whole report."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/graphql"):
            return _page("issues", [{**ISSUE_NODE, "body": "a delimiter byte \u0000 between fields"}])
        return httpx.Response(200, json={"body": "rest \u0000 too", "list": ["x\u0000y"]})

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    nodes = [n async for n in client._pages("q", {"owner": "o", "name": "r"}, "issues")]
    rest = await client.rest_get("/repos/o/r/issues/1")
    await client.aclose()
    assert nodes[0]["body"] == "a delimiter byte  between fields"
    assert rest == {"body": "rest  too", "list": ["xy"]}


async def test_a_dropped_connection_shrinks_the_page_like_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """fastapi, django, starlette: GitHub's slow answers came as ReadTimeout, which used to fail the report."""
    monkeypatch.setattr("nightshift.github.asyncio.sleep", _no_sleep)
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        first = json.loads(request.read())["variables"]["first"]
        sizes.append(first)
        if first > 20:
            raise httpx.ReadTimeout("slow", request=request)
        return _page("issues", [ISSUE_NODE])

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    nodes = [n async for n in client._pages("q", {"owner": "o", "name": "r"}, "issues")]
    await client.aclose()
    assert len(nodes) == 1 and sizes == [50, 25, 12]


async def test_a_list_github_keeps_failing_on_is_left_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closed PRs never answer; everything else does: the fetch keeps what it got and names the gap."""
    monkeypatch.setattr("nightshift.github.asyncio.sleep", _no_sleep)
    empty = {"nodes": []}
    pr_node = {**ISSUE_NODE, "number": 9, "state": "OPEN", "updatedAt": "2099-01-01T00:00:00Z", "mergedAt": None,
               "closingIssuesReferences": empty, "commits": empty, "reviews": empty}  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        if "pullRequests(" in body["query"]:
            if "MERGED" in body["variables"]["states"]:
                return httpx.Response(504, text="timeout")
            return _page("pullRequests", [pr_node])
        return _page("issues", [ISSUE_NODE])

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    items, truncated, gaps = await client.fetch_items("o/r", since=None, lookback_days=90, limit=100)
    await client.aclose()
    assert [i["kind"] for i in items] == ["issue", "issue", "pr"] and not truncated
    assert gaps == ["GitHub kept timing out on closed PRs, so only the first 0 were read"]


async def test_nothing_returned_at_all_is_still_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nightshift.github.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubError, match="GitHub is not reachable"):  # waited for, then said once
        await client.fetch_items("o/r", since=None, lookback_days=90, limit=10)
    await client.aclose()
    github.outage["since"] = None


async def test_rest_calls_retry_a_dropped_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nightshift.github.asyncio.sleep", _no_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ReadError("connection reset by peer", request=request)
        return httpx.Response(200, json={"full_name": "o/r"})

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    assert await client.canonical_name("o/r") == "o/r" and calls == 3
    await client.aclose()


async def test_an_unreachable_github_is_waited_for_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """No connection: the request waits for GitHub to answer again instead of failing (R3)."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) <= 3:  # the request, then the first two checks
            raise httpx.ConnectError("nodename nor servname provided")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(github, "PROBE_SECONDS", 0.01)
    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    assert await client.rest_get("/repos/o/r") == {"ok": True}
    assert calls == ["/repos/o/r", "/zen", "/zen", "/zen", "/repos/o/r"] and github.outage["since"] is None

    calls.clear()
    monkeypatch.setattr(get_settings(), "github_outage_seconds", 0.05)
    down = GitHubClient(
        token="t", transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    )
    with pytest.raises(GitHubError, match="GitHub is not reachable"):
        await down.graphql("query { viewer { login } }", {})
    assert github.outage["since"] is not None  # shown in the app until GitHub answers
    github.outage["since"] = None


def test_a_ci_log_is_cut_to_what_failed() -> None:
    from nightshift.github import log_excerpt

    pytest_log = "\n".join([
        "2026-10-31T06:33:19.7Z tests/test_a.py \x1b[32m.\x1b[0m\x1b[32m.\x1b[0m  [ 10%]",
        "2026-10-31T06:33:20.1Z =========== short test summary info ===========",
        "2026-10-31T06:33:20.1Z FAILED tests/test_a.py::test_x - AssertionError: assert 2 == 1",
        "2026-10-31T06:33:20.2Z ##[error]Process completed with exit code 1.",
    ])  # fmt: skip
    assert log_excerpt(pytest_log) == "FAILED tests/test_a.py::test_x - AssertionError: assert 2 == 1"
    go_log = "ok  \tpkg/a\t0.1s\n--- FAIL: TestB (0.00s)\n    b_test.go:12: expected 3, got 4\nFAIL\tpkg/b\n"
    assert "--- FAIL: TestB" in log_excerpt(go_log) and "b_test.go:12: expected 3, got 4" in log_excerpt(go_log)
    assert "ok  \tpkg/a" not in log_excerpt(go_log)


async def test_failing_jobs_are_read_and_grouped() -> None:
    from nightshift.github import ci_failures

    log = "short test summary info\nFAILED tests/test_a.py::test_x - assert 2 == 1\n"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/check-runs"):
            runs = [{"id": i, "name": name, "conclusion": c, "details_url": f"https://github.com/o/r/actions/runs/1/job/{i}",
                     "output": {"annotations_count": 0}}
                    for i, (name, c) in enumerate([("test (3.12)", "failure"), ("test (3.13)", "failure"),
                                                   ("test (windows)", "failure"), ("codecov/patch", "failure"),
                                                   ("lint", "success")], start=1)]  # fmt: skip
            return httpx.Response(200, json={"check_runs": runs})
        if "/logs" in path:
            return httpx.Response(200, text=log.replace("tests/", "tests\\") if path.split("/")[-2] == "3" else log)
        return httpx.Response(404)

    gh = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    jobs = await ci_failures(gh, "o/r", "abc")
    await gh.aclose()
    assert [j["name"] for j in jobs] == ["test (3.12), test (3.13) and 1 more"]  # codecov is advice, not a failure
    assert jobs[0]["excerpt"] == "FAILED tests/test_a.py::test_x - assert 2 == 1"
