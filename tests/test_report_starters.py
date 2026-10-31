"""A report's "good places to start" lists only issues nobody has: redis-py's listed 10, and 9 were taken (open
PRs written off as stalled, a PR that only names the issue, an assignee, a fix named only in a comment)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nightshift.db import Item
from nightshift.health import graph
from nightshift.health.contributor import prs_by_issue, work_status
from nightshift.health.report import asks_first, starters

NOW = datetime(2026, 10, 31, tzinfo=UTC)


def _item(n: int, kind: str, **kw: Any) -> Item:
    fields: dict[str, Any] = {
        "repo_id": 1, "number": n, "kind": kind, "title": f"{kind} {n}", "body": "", "author": "someone",
        "state": "OPEN", "url": f"https://github.com/o/r/issues/{n}", "labels": [],
        "created_at": NOW - timedelta(days=200), "updated_at": NOW, "data": {},
    }  # fmt: skip
    fields.update(kw)
    return Item(**fields)


def _opp(n: int, **kw: Any) -> dict[str, Any]:
    return {"number": n, "kind": "bug", "size": "small", "actionable": True, "maintainer_declined": False,
            "needs_maintainer_ok": False, "outsider_can_do": True, "claimed": False, **kw}  # fmt: skip


def test_a_pr_that_names_the_issue_is_linked_to_it() -> None:
    issue = _item(4026, "issue")
    pr = _item(4166, "pr", body="Fail fast on unrecoverable connection errors (see #4026, and o/other#4026).")
    assert [p.number for p in prs_by_issue([issue, pr])[4026]] == [4166]


def test_an_abandoned_open_pr_is_kept_apart_from_closed_attempts() -> None:
    issue = _item(3624, "issue")
    quiet = _item(4124, "pr", state="OPEN", updated_at=NOW - timedelta(days=90),
                  data={"closing_issues": [{"number": 3624, "state": "OPEN"}]})  # fmt: skip
    closed = _item(4000, "pr", state="CLOSED", data={"closing_issues": [{"number": 3624, "state": "OPEN"}]})
    by_number = {i.number: i for i in (issue, quiet, closed)}
    ws = work_status(issue, [quiet, closed], by_number, now=NOW)
    assert ws.status == "stalled" and ws.stalled_prs == [4000, 4124] and ws.open_stalled == [4124]


def test_an_open_pr_means_ask_first_not_start_here() -> None:
    opps = [_opp(1), _opp(2, open_prs=[4124], status="stalled"), _opp(3, status="stalled", stalled_prs=[4000])]
    assert [o["number"] for o in starters(opps)] == [3, 1]  # closed attempts can still be picked up
    assert asks_first(opps[1]) and not asks_first(opps[2])


async def test_starters_are_rechecked_on_github_before_the_report_is_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    class View:
        def __init__(self, why: list[str]) -> None:
            self.why, self.linked_prs = why, []

        def blockers(self, operator: str | None) -> list[str]:
            return self.why

    async def fetch_issue(repo: str, number: int) -> View:
        if number == 3:
            raise ConnectionError("GitHub is not reachable")
        if number == 4:
            return View(["merged PR #4127 by LOGESHWAR123 does its work, says a comment on #4165"])
        return View(["assigned to petyaslavova"] if number == 2 else [])

    monkeypatch.setattr(graph, "fetch_issue", fetch_issue)
    opps, worked_on = [_opp(1), _opp(2), _opp(3), _opp(4, needs_maintainer_ok=True)], []
    not_checked = await graph.recheck_starters("o/r", opps, worked_on)
    assert not_checked == 1
    assert [o["number"] for o in starters(opps)] == [3, 1]  # #2 is taken now; #3 kept, but unchecked
    assert sorted(w["issue"] for w in worked_on) == [2, 4]  # an "ask first" issue already fixed is not listed either
    assert {"issue": 2, "prs": [], "source": "checked", "why": "assigned to petyaslavova"} in worked_on


async def test_an_issue_held_only_by_an_abandoned_pr_stays_ask_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from nightshift.worker.issue import LinkedPR

    class View:
        linked_prs = [LinkedPR(number=4204, author="Brumbelow", state="open", title="fix", how="will close it")]

        def blockers(self, operator: str | None) -> list[str]:
            return ["open PR #4204 by Brumbelow will close it"]

    async def fetch_issue(repo: str, number: int) -> View:
        return View()

    monkeypatch.setattr(graph, "fetch_issue", fetch_issue)
    abandoned, active = _opp(1, open_prs=[4204], status="stalled"), _opp(2)
    worked_on: list[dict[str, Any]] = []
    await graph.recheck_starters("o/r", [abandoned, active], worked_on)
    assert not abandoned["claimed"] and asks_first(abandoned)  # you can ask to take the abandoned PR over
    assert active["claimed"] and [w["issue"] for w in worked_on] == [2]
