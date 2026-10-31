"""Contributor view: facts an outsider needs before spending time on a repo. Computed by code, no model.

- pulse: do outside PRs get merged, and how fast do maintainers first respond?
- work status: for each open issue, is someone already on it, did an attempt stall, or is it free?
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from nightshift.db import Item
from nightshift.health.signals import pr_signals

MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
# GraphQL reports app accounts without the "[bot]" suffix, so match known names as well as the "Bot" type.
KNOWN_BOTS = {"github-actions", "dependabot", "renovate", "copilot", "pre-commit-ci", "codecov", "allcontributors"}
WAITING_AFTER_DAYS = 7  # an outside PR with no maintainer response after this long counts as unanswered
STALLED_AFTER_DAYS = 60
CLAIM_FRESH_DAYS = 90  # a claim older than this, with no PR, no longer counts

# A fix already written on someone's fork: github.com/<owner>/<repo>/commit|tree|compare/...
FORK_FIX_RE = re.compile(r"https://github\.com/([\w.-]+)/[\w.-]+/(?:commit|tree|compare)/", re.IGNORECASE)
CLAIM_TEXT_RE = re.compile(
    r"\b(?:i(?:'m| am) (?:working on|taking|on) (?:this|it)"
    r"|i(?:'ll| will) (?:take|pick up|work on|fix|handle) (?:this|it)"
    r"|(?:here(?:'s| is)|i have|i've got) a (?:fix|patch)"
    r"|i have (?:this|it|a fix|a patch|the fix) (?:running|working|ready|locally)"
    r"|(?:happy|glad|willing) to (?:open|submit|send|raise) (?:a |the )?(?:pr|pull request)"
    r"|i (?:can|could|will|'ll) (?:open|submit|send|raise) (?:a |the )?(?:pr|pull request)"
    r"|assign (?:this|it) to me)\b",
    re.IGNORECASE,
)


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def is_bot(login: str | None, author_type: str | None = None) -> bool:
    if author_type == "Bot":
        return True
    if not login:
        return False
    name = login.lower().removesuffix("[bot]")
    return login.endswith("[bot]") or name.endswith("-bot") or name in KNOWN_BOTS


def maintainers(items: list[Item]) -> set[str]:
    """People who have merged a PR. Catches maintainers whose org membership is private, whom GitHub
    reports to outside tokens as CONTRIBUTOR or NONE."""
    return {m for it in items if it.kind == "pr" and (m := it.data.get("merged_by"))}


def _is_maintainer(login: str | None, association: str | None, known: set[str]) -> bool:
    return association in MAINTAINER_ASSOCIATIONS or (login is not None and login in known)


def is_outside(item: Item, known: set[str] | None = None) -> bool:
    """Opened by someone who is neither a maintainer nor a bot."""
    if is_bot(item.author, item.data.get("author_type")):
        return False
    return not _is_maintainer(item.author, item.data.get("author_association"), known or set())


def first_maintainer_response(item: Item, known: set[str] | None = None) -> datetime | None:
    """When a maintainer first commented on or reviewed the item (None if never, as far as we can see)."""
    responses = list(item.data.get("first_responses", []))
    responses += [
        {"author": c.get("author"), "association": c.get("association"), "at": c.get("created_at")}
        for c in item.data.get("comments", [])
    ]
    times = [
        t
        for r in responses
        if _is_maintainer(r.get("author"), r.get("association"), known or set())
        and r.get("author") != item.author
        and (t := _parse(r.get("at"))) is not None
    ]
    return min(times, default=None)


@dataclass
class Pulse:
    window_days: int
    outside_prs_merged: int
    outside_prs_closed_unmerged: int
    outside_prs_open: int
    merge_rate: float | None  # merged / (merged + closed unmerged), outside PRs only
    median_pr_response_days: float | None
    median_issue_response_days: float | None
    outside_prs_waiting: int  # open, older than WAITING_AFTER_DAYS, never answered by a maintainer
    last_merge: str | None
    verdict: str  # welcoming | slow | hard | inactive
    verdict_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _response_days(items: list[Item], known: set[str]) -> float | None:
    days = []
    for it in items:
        first = first_maintainer_response(it, known)
        if first is not None:
            days.append(max(0.0, (first - it.created_at).total_seconds() / 86400))
    return round(median(days), 1) if days else None


MIN_PRS_TO_JUDGE = 3


def pulse(items: list[Item], window_days: int, now: datetime | None = None) -> Pulse:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=window_days)
    known = maintainers(items)
    recent = [it for it in items if it.created_at >= since and is_outside(it, known)]
    prs = [it for it in recent if it.kind == "pr"]
    issues = [it for it in recent if it.kind == "issue"]

    merged = sum(1 for p in prs if p.state == "MERGED")
    closed = sum(1 for p in prs if p.state == "CLOSED")
    open_ = sum(1 for p in prs if p.state == "OPEN")
    rate = merged / (merged + closed) if merged + closed else None
    waiting = sum(
        1
        for p in prs
        if p.state == "OPEN"
        and (now - p.created_at).days >= WAITING_AFTER_DAYS
        and first_maintainer_response(p, known) is None
    )
    merges = [t for it in items if it.kind == "pr" and (t := _parse(it.data.get("merged_at")))]
    last_merge = max(merges, default=None)
    pr_resp = _response_days(prs, known)
    issue_resp = _response_days(issues, known)

    facts = f"{merged} of {len(prs)} outside PRs from the last {window_days} days were merged"
    facts += f", {closed} closed unmerged, {waiting} still waiting for any maintainer response."
    waiting_share = waiting / len(prs) if prs else 0.0
    if last_merge is None or (now - last_merge).days > window_days:
        verdict, reason = "inactive", f"No PR merged in the last {window_days} days."
    elif len(prs) < MIN_PRS_TO_JUDGE:
        verdict, reason = "slow", f"Too few outside PRs to judge ({len(prs)} in {window_days} days)."
    elif merged == 0 or (rate is not None and rate < 0.25) or waiting_share >= 0.5:
        verdict, reason = "hard", facts
    elif rate is not None and rate >= 0.5 and pr_resp is not None and pr_resp <= 7 and waiting_share < 0.25:
        verdict, reason = "welcoming", f"{facts} First maintainer response in about {pr_resp} days."
    else:
        verdict, reason = "slow", facts
    return Pulse(
        window_days=window_days,
        outside_prs_merged=merged,
        outside_prs_closed_unmerged=closed,
        outside_prs_open=open_,
        merge_rate=round(rate, 2) if rate is not None else None,
        median_pr_response_days=pr_resp,
        median_issue_response_days=issue_resp,
        outside_prs_waiting=waiting,
        last_merge=last_merge.date().isoformat() if last_merge else None,
        verdict=verdict,
        verdict_reason=reason,
    )


@dataclass
class WorkStatus:
    number: int
    status: str  # taken | claimed | stalled | free
    active_prs: list[int] = field(default_factory=list)
    stalled_prs: list[int] = field(default_factory=list)
    open_stalled: list[int] = field(default_factory=list)  # open PRs among stalled_prs: someone still has one
    claimed_by: str | None = None  # someone linked a fix from their fork or said they are taking it


def claimed_by(issue: Item, now: datetime | None = None) -> str | None:
    """Who has claimed the issue in its text or comments, without a PR yet (None if nobody, recently)."""
    now = now or datetime.now(UTC)
    repo_owner = issue.url.split("/")[3].lower() if issue.url.count("/") >= 4 else ""
    body = issue.body + "\n" + issue.data.get("body_tail", "")
    posts: list[tuple[str | None, str, datetime | None]] = [(issue.author, body, issue.created_at)]
    posts += [
        (c.get("author"), c.get("body") or "", _parse(c.get("created_at"))) for c in issue.data.get("comments", [])
    ]
    for author, text, at in reversed(posts):  # most recent claim wins
        if at is not None and (now - at).days > CLAIM_FRESH_DAYS:
            continue
        fork_owners = {m.lower() for m in FORK_FIX_RE.findall(text)} - {repo_owner}
        if fork_owners or CLAIM_TEXT_RE.search(text):
            return author or "someone"
    return None


def work_status(
    issue: Item, prs_for_issue: list[Item], by_number: dict[int, Item], now: datetime | None = None
) -> WorkStatus:
    """Is someone already working on this issue? A PR counts if it closes or references the issue."""
    now = now or datetime.now(UTC)
    active: list[int] = []
    stalled: list[int] = []
    open_stalled: list[int] = []
    for pr in prs_for_issue:
        if pr.state == "MERGED":
            continue  # merged but issue still open: partial fix, not someone "on it"
        if pr.state == "CLOSED":
            stalled.append(pr.number)
            continue
        sig = pr_signals(pr, by_number, STALLED_AFTER_DAYS, now=now)
        if sig.looks_abandoned or sig.idle_days >= STALLED_AFTER_DAYS:
            stalled.append(pr.number)
            open_stalled.append(pr.number)
        else:
            active.append(pr.number)
    claimant = None if active else claimed_by(issue, now)
    status = "taken" if active else "claimed" if claimant else "stalled" if stalled else "free"
    return WorkStatus(issue.number, status, sorted(active), sorted(stalled), sorted(open_stalled), claimant)


ISSUE_MENTION = re.compile(r"(?<![\w/])#(\d+)\b")


def prs_by_issue(items: list[Item]) -> dict[int, list[Item]]:
    """Map issue number -> PRs that close it, cross-reference it (in either direction), or name it as `#N` in
    their title or description (redis-py #4166 names #4026 without closing or cross-referencing it)."""
    by_number = {it.number: it for it in items}
    out: dict[int, dict[int, Item]] = {}
    for pr in (it for it in items if it.kind == "pr"):
        refs = {c["number"] for c in pr.data.get("closing_issues", [])} | set(pr.data.get("cross_refs", []))
        refs |= {int(n) for n in ISSUE_MENTION.findall(f"{pr.title}\n{pr.body}")}
        for n in refs:
            if (target := by_number.get(n)) is not None and target.kind == "issue":
                out.setdefault(n, {})[pr.number] = pr
    for issue in (it for it in items if it.kind == "issue"):
        for n in issue.data.get("cross_refs", []):
            if (linked := by_number.get(n)) is not None and linked.kind == "pr":
                out.setdefault(issue.number, {})[linked.number] = linked
    return {n: list(prs.values()) for n, prs in out.items()}
