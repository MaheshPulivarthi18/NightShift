from __future__ import annotations

from datetime import datetime, timedelta

from conftest import make_item

from nightshift.health.contributor import (
    claimed_by,
    first_maintainer_response,
    is_outside,
    maintainers,
    prs_by_issue,
    pulse,
    work_status,
)


def _at(now: datetime, days_ago: float) -> str:
    return (now - timedelta(days=days_ago)).isoformat()


def test_outside_excludes_maintainers_and_bots() -> None:
    assert is_outside(make_item(1, author_association="CONTRIBUTOR"))
    assert not is_outside(make_item(2, author_association="MEMBER"))
    bot = make_item(3, author_association="NONE")
    bot.author = "dependabot[bot]"
    assert not is_outside(bot)


def test_first_response_ignores_the_author_and_non_maintainers(now: datetime) -> None:
    pr = make_item(
        1,
        first_responses=[
            {"author": "alice", "association": "OWNER", "at": _at(now, 9)},  # the author themself
            {"author": "bob", "association": "NONE", "at": _at(now, 8)},  # not a maintainer
            {"author": "carol", "association": "MEMBER", "at": _at(now, 5)},
        ],
    )
    assert first_maintainer_response(pr) == now - timedelta(days=5)


def test_pulse_welcoming(now: datetime) -> None:
    items = []
    for n in range(4):
        pr = make_item(
            n,
            state="MERGED",
            days_old=20,
            author_association="CONTRIBUTOR",
            merged_at=_at(now, 10),
            first_responses=[{"author": "m", "association": "OWNER", "at": _at(now, 19)}],
        )
        items.append(pr)
    p = pulse(items, window_days=90, now=now)
    assert p.verdict == "welcoming"
    assert p.outside_prs_merged == 4 and p.merge_rate == 1.0
    assert p.median_pr_response_days == 1.0


def test_pulse_hard_when_outside_prs_wait_unanswered(now: datetime) -> None:
    merged_own = make_item(1, state="MERGED", days_old=5, author_association="OWNER", merged_at=_at(now, 4))
    waiting = [make_item(n, days_old=30, author_association="NONE") for n in range(2, 6)]
    p = pulse([merged_own, *waiting], window_days=90, now=now)
    assert p.verdict == "hard"
    assert p.outside_prs_waiting == 4


def test_pulse_inactive_without_recent_merges(now: datetime) -> None:
    old = make_item(1, state="MERGED", days_old=200, merged_at=_at(now, 199))
    assert pulse([old], window_days=90, now=now).verdict == "inactive"


def test_work_status(now: datetime) -> None:
    issue = make_item(10, kind="issue")
    active = make_item(11, days_old=3, closing_issues=[{"number": 10, "state": "OPEN"}])
    closed = make_item(12, state="CLOSED", closing_issues=[{"number": 10, "state": "OPEN"}])
    by_number = {i.number: i for i in (issue, active, closed)}
    linked = prs_by_issue(list(by_number.values()))
    assert {p.number for p in linked[10]} == {11, 12}

    ws = work_status(issue, linked[10], by_number, now=now)
    assert ws.status == "taken" and ws.active_prs == [11] and ws.stalled_prs == [12]

    ws = work_status(issue, [closed], by_number, now=now)
    assert ws.status == "stalled"
    assert work_status(issue, [], by_number, now=now).status == "free"


def test_bots_detected_by_type_and_name() -> None:
    gha = make_item(1, author_association="CONTRIBUTOR", author_type="Bot")
    gha.author = "github-actions"  # GraphQL gives no [bot] suffix
    assert not is_outside(gha)
    named = make_item(2, author_association="NONE")
    named.author = "renovate"
    assert not is_outside(named)


def test_people_who_merge_count_as_maintainers(now: datetime) -> None:
    # A staff member with private org membership shows up as CONTRIBUTOR, but they merge PRs.
    merged = make_item(1, state="MERGED", author_association="CONTRIBUTOR", merged_by="staffer")
    staff_pr = make_item(2, author_association="CONTRIBUTOR")
    staff_pr.author = "staffer"
    known = maintainers([merged, staff_pr])
    assert known == {"staffer"}
    assert not is_outside(staff_pr, known)

    issue = make_item(
        3, kind="issue", first_responses=[{"author": "staffer", "association": "CONTRIBUTOR", "at": _at(now, 2)}]
    )
    assert first_maintainer_response(issue) is None
    assert first_maintainer_response(issue, known) == now - timedelta(days=2)


def test_claims_from_fork_links_and_comments(now: datetime) -> None:
    issue = make_item(20, kind="issue")
    issue.url = "https://github.com/acme/tool/issues/20"
    issue.body = "Bug. Patch: https://github.com/oxura/tool/commit/77ded14"
    issue.author = "oxura"
    assert claimed_by(issue, now) == "oxura"

    own_commit = make_item(21, kind="issue")  # links the repo's own commit: a regression note, not a claim
    own_commit.url = "https://github.com/acme/tool/issues/21"
    own_commit.body = "Regressed in https://github.com/acme/tool/commit/abc123"
    assert claimed_by(own_commit, now) is None

    comment = make_item(
        22,
        kind="issue",
        comments=[{"id": 1, "author": "bob", "body": "I'll take this one", "created_at": _at(now, 3)}],
    )
    assert claimed_by(comment, now) == "bob"
    assert work_status(comment, [], {22: comment}, now=now).status == "claimed"

    stale = make_item(
        23,
        kind="issue",
        comments=[{"id": 2, "author": "bob", "body": "I'm working on this", "created_at": _at(now, 200)}],
    )
    assert claimed_by(stale, now) is None


def test_claim_phrases_offering_a_pr(now: datetime) -> None:
    for text in (
        "I have this running locally as a ~60-line patch. Happy to open a PR if the approach looks right.",
        "I can submit a pull request for this.",
    ):
        issue = make_item(30, kind="issue")
        issue.body = text
        assert claimed_by(issue, now) == "alice", text
    plain = make_item(31, kind="issue")
    plain.body = "Steps to reproduce: open a PR and watch CI fail."
    assert claimed_by(plain, now) is None
