from __future__ import annotations

from datetime import datetime, timedelta

from conftest import make_item

from nightshift.health.signals import pr_signals


def test_superseded_by_merged_pr_closing_same_issue(now: datetime) -> None:
    old = make_item(10, closing_issues=[{"number": 5, "state": "OPEN", "state_reason": None}])
    newer = make_item(12, state="MERGED", closing_issues=[{"number": 5, "state": "OPEN", "state_reason": None}])
    sig = pr_signals(old, {10: old, 12: newer}, abandoned_after_days=60, now=now)
    assert sig.superseded_by == [12]


def test_older_open_pr_does_not_supersede_newer(now: datetime) -> None:
    older = make_item(8, days_old=30, closing_issues=[{"number": 5, "state": "OPEN", "state_reason": None}])
    newer = make_item(12, days_old=5, closing_issues=[{"number": 5, "state": "OPEN", "state_reason": None}])
    sig = pr_signals(newer, {8: older, 12: newer}, abandoned_after_days=60, now=now)
    assert sig.superseded_by == []


def test_linked_issue_fixed_elsewhere_and_not_planned(now: datetime) -> None:
    issue_fixed = make_item(5, kind="issue", state="CLOSED", closed_by={"type": "pr", "number": 99})
    pr = make_item(
        10,
        closing_issues=[
            {"number": 5, "state": "CLOSED", "state_reason": "COMPLETED"},
            {"number": 6, "state": "CLOSED", "state_reason": "NOT_PLANNED"},
        ],
    )
    sig = pr_signals(pr, {5: issue_fixed, 10: pr}, abandoned_after_days=60, now=now)
    assert sig.linked_issues_fixed_elsewhere == [{"issue": 5, "type": "pr", "number": 99}]
    assert sig.linked_issues_not_planned == [6]


def test_issue_closed_by_this_pr_is_not_fixed_elsewhere(now: datetime) -> None:
    issue = make_item(5, kind="issue", state="CLOSED", closed_by={"type": "pr", "number": 10})
    pr = make_item(10, closing_issues=[{"number": 5, "state": "CLOSED", "state_reason": "COMPLETED"}])
    assert pr_signals(pr, {5: issue, 10: pr}, 60, now=now).linked_issues_fixed_elsewhere == []


def test_abandoned_needs_inactivity_and_conflict_or_unanswered_review(now: datetime) -> None:
    stale_commit = (now - timedelta(days=90)).isoformat()
    review = [{"state": "COMMENTED", "author": "maint", "submitted_at": stale_commit, "body": "nit"}]
    pr = make_item(10, days_old=120, mergeable="CONFLICTING", last_commit_at=stale_commit, reviews=review)
    assert pr_signals(pr, {10: pr}, 60, now=now).looks_abandoned

    clean = make_item(11, days_old=120, mergeable="MERGEABLE", last_commit_at=stale_commit, reviews=review)
    assert not pr_signals(clean, {11: clean}, 60, now=now).looks_abandoned


def test_never_reviewed_pr_is_not_abandoned(now: datetime) -> None:
    stale_commit = (now - timedelta(days=90)).isoformat()
    pr = make_item(12, days_old=120, mergeable="CONFLICTING", last_commit_at=stale_commit)
    sig = pr_signals(pr, {12: pr}, 60, now=now)
    assert sig.never_reviewed and not sig.looks_abandoned


def test_changes_requested_unanswered(now: datetime) -> None:
    review_at = (now - timedelta(days=3)).isoformat()
    pr = make_item(
        10,
        days_old=20,
        last_commit_at=(now - timedelta(days=10)).isoformat(),
        reviews=[{"state": "CHANGES_REQUESTED", "author": "maint", "submitted_at": review_at, "body": ""}],
    )
    assert pr_signals(pr, {10: pr}, 60, now=now).changes_requested_unanswered


def test_ready_to_merge(now: datetime) -> None:
    pr = make_item(
        10, days_old=20, ci_state="SUCCESS", mergeable="MERGEABLE", additions=10, deletions=2, is_draft=False
    )
    assert pr_signals(pr, {10: pr}, 60, now=now).looks_ready

    big = make_item(11, days_old=20, ci_state="SUCCESS", mergeable="MERGEABLE", additions=500, deletions=0)
    assert not pr_signals(big, {11: big}, 60, now=now).looks_ready
