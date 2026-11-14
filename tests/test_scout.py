"""Scouting repos you do not watch yet, from a recorded GitHub answer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nightshift.worker.scout import guide_notes, rank, row

NOW = datetime(2026, 11, 14, tzinfo=UTC)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(
    n: int, state: str, author: str, association: str, days_ago: float, *, bot_note: str = "", typename: str = "User"
) -> dict:
    return {
        "number": n, "state": state, "createdAt": iso(days_ago), "authorAssociation": association,
        "mergedAt": iso(days_ago - 1) if state == "MERGED" else None,
        "closedAt": iso(days_ago - 1) if state != "OPEN" else None,
        "author": {"__typename": typename, "login": author},
        "mergedBy": {"login": "boss"} if state == "MERGED" else None,
        "firstComments": {"nodes": [{"author": {"login": "boss"}, "authorAssociation": "MEMBER",
                                     "createdAt": iso(days_ago - 0.5)}]},
        "firstReviews": {"nodes": []},
        "lastComments": {"nodes": [{"body": bot_note}] if bot_note else []},
    }  # fmt: skip


def repo(prs: list[dict], guide: str = "") -> dict:
    return {
        "nameWithOwner": "acme/tool", "description": "A tool", "stargazerCount": 5000, "diskUsage": 20480,
        "isArchived": False, "pushedAt": iso(1), "primaryLanguage": {"name": "Python"},
        "good": {"totalCount": 4}, "help": {"totalCount": 2},
        "c1": {"text": guide} if guide else None, "c2": None, "c3": None,
        "pullRequests": {"nodes": prs},
    }  # fmt: skip


def test_outside_prs_counted_and_bots_and_members_left_out() -> None:
    prs = [
        pr(1, "MERGED", "alice", "CONTRIBUTOR", 30),
        pr(2, "MERGED", "bob", "NONE", 20),
        pr(3, "CLOSED", "carol", "NONE", 10),
        pr(4, "MERGED", "boss", "MEMBER", 5),
        pr(5, "MERGED", "dependabot", "CONTRIBUTOR", 5, typename="Bot"),
        pr(6, "MERGED", "old", "NONE", 200),  # before the window
    ]
    r = row(repo(prs), NOW)
    p = r["pulse"]
    assert (p["outside_prs_merged"], p["outside_prs_closed_unmerged"]) == (2, 1)
    assert r["size_mb"] == 20 and r["good_first"] == 4 and r["help_wanted"] == 2
    assert r["covers_days"] == 90


def test_prs_landed_by_copybara_count_as_merged() -> None:
    note = "Your changes have been successfully imported and merged via Copybara in commit 9687bae."
    prs = [pr(1, "CLOSED", "alice", "NONE", 10, bot_note=note), pr(2, "CLOSED", "bob", "NONE", 9, bot_note="Stale.")]
    p = row(repo(prs), NOW)["pulse"]
    assert (p["outside_prs_merged"], p["outside_prs_closed_unmerged"]) == (1, 1)


def test_coverage_is_how_far_back_the_prs_go() -> None:
    assert row(repo([pr(1, "OPEN", "a", "NONE", 6)]), NOW)["covers_days"] == 6


def test_guide_notes_find_a_cla_and_ai_lines() -> None:
    guide = (
        "# Contributing\n\nYou must sign the Contributor License Agreement (CLA) first.\n\n"
        "- Pull requests generated with AI tools must say so in the description.\nUse ruff."
    )
    notes = guide_notes(guide)
    assert notes["cla"] is True
    assert notes["ai_policy"] == ["Pull requests generated with AI tools must say so in the description."]
    assert guide_notes("Use ruff.") == {"cla": False, "ai_policy": []}


def test_rank_prefers_merges_then_fit_and_sinks_the_rest() -> None:
    good = {"pulse": {"merge_rate": 0.8, "verdict": "welcoming"}, "fit": {"score": 1.0}}
    hard = {"pulse": {"merge_rate": 0.8, "verdict": "hard"}, "fit": {"score": 1.0}}
    unknown = {"pulse": {"merge_rate": None, "verdict": "slow"}, "fit": {"score": 1.0}}
    archived = {**good, "archived": True}
    assert rank(good) > rank(hard) > rank(unknown) > rank(archived)
