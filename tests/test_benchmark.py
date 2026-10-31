"""The PR checker benchmark's scoring: its verdict against what the maintainers did."""

from __future__ import annotations

from nightshift.worker.benchmark import Case, summary, to_markdown


def _case(n: int, outcome: str, blocked: bool | None, **kw: object) -> Case:
    return Case(number=n, url=f"https://github.com/o/r/pull/{n}", title=f"PR {n}", author="a", outcome=outcome,
                blocked=blocked, **kw)  # type: ignore[arg-type]  # fmt: skip


def test_the_verdict_is_scored_against_the_maintainers() -> None:
    cases = [
        _case(1, "merged", False, seconds=120, cost_usd=0.2),
        _case(2, "merged", True, must=["the targeted test FAILS with the fix"], seconds=60, cost_usd=0.1),
        _case(3, "closed", True, ci="failed", must=["these passed before the change and fail now: pytest"]),
        _case(4, "closed", False, closing_note="Duplicate of #10, thanks!"),
        _case(5, "closed", None, error="SandboxError: cannot fetch the PR"),
    ]
    s = summary(cases)
    assert (s["checked"], s["failed_to_check"], s["agree"]) == (4, 1, 2)
    assert (s["merged_ready"], s["merged"], s["closed_blocked"], s["closed"]) == (1, 2, 1, 2)
    assert (s["blocks_right"], s["blocked"], s["ci_failed_blocked"], s["ci_failed"]) == (1, 2, 1, 1)
    md = to_markdown("o/r", cases, 90)
    assert "| Verdict matches the maintainers | 2 of 4 (50%) |" in md
    assert "✗ blocked | the targeted test FAILS with the fix" in md
    assert "✗ ready | closed: Duplicate of #10, thanks!" in md
    assert "could not check | SandboxError: cannot fetch the PR" in md


def test_an_empty_run_says_so() -> None:
    assert "| Verdict matches the maintainers | none yet |" in to_markdown("o/r", [], 90)


def test_a_pr_closed_by_a_bot_is_the_repos_policy_and_not_scored() -> None:
    """pydantic auto-closes PRs whose author is not assigned to the issue: no code check can agree or disagree."""
    cases = [
        _case(1, "merged", False),
        _case(2, "closed", True, closed_by="github-actions[bot]"),
        _case(3, "closed", True, closed_by="Viicos"),
    ]
    s = summary(cases)
    assert (s["checked"], s["scored"], s["by_policy"], s["policy_blocked"]) == (3, 2, 1, 1)
    assert (s["agree"], s["closed_blocked"], s["closed"]) == (2, 1, 1)
    md = to_markdown("o/r", cases, 90)
    assert "| Verdict matches the maintainers | 2 of 2 (100%) |" in md
    assert "closed by github-actions[bot] | none | · blocked" in md


def test_a_check_that_could_not_run_is_not_scored() -> None:
    cases = [_case(1, "merged", False), _case(2, "closed", False, incomplete=True), _case(3, "closed", True,
             incomplete=True)]  # fmt: skip
    s = summary(cases)
    assert (s["scored"], s["not_fully_checked"], s["agree"]) == (2, 1, 2)  # a blocked one still counts
    assert "· not fully checked" in to_markdown("o/r", cases, 90)
