"""Facts about pull requests that code can establish without a model.

Agents get these as ground truth; they judge, but they never have to guess a date,
a merge state or a CI result.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from nightshift.db import Item

READY_MAX_LINES = 200
READY_MIN_IDLE_DAYS = 7


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _days(since: datetime | None, now: datetime) -> int | None:
    return None if since is None else max(0, (now - since).days)


@dataclass
class PRSignals:
    number: int
    lines_changed: int
    idle_days: int
    author_inactive_days: int | None
    is_draft: bool
    has_conflicts: bool
    ci_state: str | None
    review_decision: str | None
    changes_requested_unanswered: bool
    never_reviewed: bool = False
    linked_issues_not_planned: list[int] = field(default_factory=list)
    linked_issues_fixed_elsewhere: list[dict[str, Any]] = field(default_factory=list)
    superseded_by: list[int] = field(default_factory=list)
    looks_abandoned: bool = False
    looks_ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def author_last_activity(pr: Item) -> datetime | None:
    stamps = [pr.created_at, _parse(pr.data.get("last_commit_at"))]
    stamps += [_parse(c["created_at"]) for c in pr.data.get("comments", []) if c.get("author") == pr.author]
    return max((s for s in stamps if s is not None), default=None)


def pr_signals(
    pr: Item, by_number: dict[int, Item], abandoned_after_days: int, now: datetime | None = None
) -> PRSignals:
    now = now or datetime.now(UTC)
    d = pr.data
    last_author = author_last_activity(pr)

    requested = [
        ts
        for r in d.get("reviews", [])
        if r["state"] == "CHANGES_REQUESTED" and (ts := _parse(r.get("submitted_at"))) is not None
    ]
    changes_requested_at = max(requested, default=None)
    unanswered = bool(changes_requested_at and (last_author is None or last_author <= changes_requested_at))

    not_planned: list[int] = []
    fixed_elsewhere: list[dict[str, Any]] = []
    for ref in d.get("closing_issues", []):
        if ref["state"] != "CLOSED":
            continue
        if ref.get("state_reason") == "NOT_PLANNED":
            not_planned.append(ref["number"])
            continue
        issue = by_number.get(ref["number"])
        closer = (issue.data.get("closed_by") if issue else None) or {}
        if closer and not (closer.get("type") == "pr" and closer.get("number") == pr.number):
            fixed_elsewhere.append({"issue": ref["number"], **closer})

    # A newer or merged PR that closes the same issue supersedes this one.
    my_issues = {i["number"] for i in d.get("closing_issues", [])}
    superseded = sorted(
        other.number
        for other in by_number.values()
        if other.kind == "pr"
        and other.number != pr.number
        and my_issues & {i["number"] for i in other.data.get("closing_issues", [])}
        and (other.state == "MERGED" or (other.state == "OPEN" and other.created_at > pr.created_at))
    )
    if (dup := d.get("duplicate_of")) and dup in by_number and by_number[dup].kind == "pr":
        superseded = sorted(set(superseded) | {dup})

    lines = (d.get("additions") or 0) + (d.get("deletions") or 0)
    idle = _days(pr.updated_at, now) or 0
    inactive = _days(last_author, now)
    conflicts = d.get("mergeable") == "CONFLICTING"

    # Nobody but the author ever responded: the PR is waiting on maintainers, so it is not the author's to abandon.
    never_reviewed = not d.get("reviews") and not any(c.get("author") != pr.author for c in d.get("comments", []))
    abandoned = bool(
        inactive is not None and inactive >= abandoned_after_days and (unanswered or (conflicts and not never_reviewed))
    )
    ready = (
        not d.get("is_draft")
        and d.get("ci_state") == "SUCCESS"
        and d.get("mergeable") == "MERGEABLE"
        and d.get("review_decision") != "CHANGES_REQUESTED"
        and lines <= READY_MAX_LINES
        and idle >= READY_MIN_IDLE_DAYS
    )
    return PRSignals(
        number=pr.number,
        lines_changed=lines,
        idle_days=idle,
        author_inactive_days=inactive,
        is_draft=bool(d.get("is_draft")),
        has_conflicts=conflicts,
        ci_state=d.get("ci_state"),
        review_decision=d.get("review_decision"),
        changes_requested_unanswered=unanswered,
        never_reviewed=never_reviewed,
        linked_issues_not_planned=not_planned,
        linked_issues_fixed_elsewhere=fixed_elsewhere,
        superseded_by=superseded,
        looks_abandoned=abandoned,
        looks_ready=ready,
    )
