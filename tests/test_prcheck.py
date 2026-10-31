"""The PR checker's code-made judgements: linked issues, evidence, and how verification becomes findings."""

from __future__ import annotations

from typing import Any

import pytest

from nightshift.worker import prcheck
from nightshift.worker.prcheck import PullRequest, Report, check_evidence, linked_issue_number
from nightshift.worker.verify import CommandResult, FixCheck


def _pr(**kw: Any) -> PullRequest:
    base: dict[str, Any] = dict(
        repo="o/r", number=5, url="https://github.com/o/r/pull/5", title="fix: a", body="", author="someone",
        state="open", draft=False, base_ref="main", base_sha="b" * 40, head_sha="h" * 40, files=["src/a.py"],
    )  # fmt: skip
    return PullRequest(**(base | kw))


def _run(ok: bool, command: str = "pytest -q") -> CommandResult:
    return CommandResult(command=command, ok=ok, exit_code=0 if ok else 1, timed_out=False, seconds=1, tail="")


def test_the_linked_issue_comes_from_closing_keywords() -> None:
    assert linked_issue_number(_pr(body="This change fixes #12 by ...")) == 12
    assert linked_issue_number(_pr(body="Closes: #7")) == 7
    assert linked_issue_number(_pr(body="See #9 for context")) is None  # a mention is not a link


def test_evidence() -> None:
    bare = Report(pr=_pr())
    check_evidence(bare.pr, bare)
    assert any("no test" in s for s in bare.should) and any("no test run" in s for s in bare.should)
    proven = Report(pr=_pr(files=["src/a.py", "tests/test_a.py"], body="```\n12 passed in 0.4s\n```"))
    check_evidence(proven.pr, proven)
    assert not proven.should and proven.passed == ["Tests changed: tests/test_a.py."]
    docs = Report(pr=_pr(files=["README.md"]))
    check_evidence(docs.pr, docs)
    assert not docs.should  # a docs change needs no test


def test_verification_becomes_findings() -> None:
    report = Report(pr=_pr())
    good = FixCheck(
        passed=True, test_files=["tests/t.py"], source_files=["a.py"], fails_without_fix=_run(False),
        passes_with_fix=_run(True), suite=[_run(True), _run(False, "pytest slow")], already_failing=["pytest slow"],
        reproduction=_run(True, "python repro.py"),
    )  # fmt: skip
    prcheck._findings(good, report, {"command": "python repro.py"})
    assert not report.must
    assert "The PR's tests fail without its source changes and pass with them." in report.passed
    assert "The linked issue's reproduction fails without the PR and passes with it." in report.passed
    assert "No regressions: 1 of 2 suites pass (1 already failing on the base)." in report.passed

    bad = FixCheck(
        passed=False, test_files=["tests/t.py"], source_files=["a.py"],
        problems=["the targeted test PASSES without the fix, so it does not reproduce the issue",
                  "these passed before the change and fail now: pytest -q"],
    )  # fmt: skip
    report = Report(pr=_pr())
    prcheck._findings(bad, report, None)
    assert report.must == [
        "The targeted test PASSES without the fix, so it does not reproduce the issue.",
        "These passed before the change and fail now: pytest -q.",
    ]
    assert report.verdict == "Needs work before review"


def test_the_report_reads_top_down() -> None:
    report = Report(pr=_pr(), must=["Fix X."], should=["Link the issue."], passed=["Template complete."])
    text = report.markdown()
    assert text.startswith("# PR check: o/r#5 fix: a\n\n**Needs work before review.**")
    assert text.index("## Must fix") < text.index("## Should fix") < text.index("## Checked and fine")
    assert Report(pr=_pr()).verdict == "Ready for a maintainer's review"


async def test_a_bad_url_is_refused() -> None:
    with pytest.raises(prcheck.PRCheckError, match="Not a pull request URL"):
        await prcheck.fetch_pull("https://github.com/o/r/issues/5")


def test_errors_in_a_report_carry_no_account_details() -> None:
    from nightshift.llm import ModelAPIError

    e = ModelAPIError(402, "This request requires more credits. To increase, visit https://openrouter.ai/keys/abc123")
    assert prcheck._short(e) == "ModelAPIError: 402: This request requires more credits"


def test_the_verification_summary_quotes_results_not_logs() -> None:
    log = "collected 9 items\n" + "log line\n" * 50 + "E   assert 1 == 2\n1 failed, 8 passed in 0.1s"
    noisy = CommandResult(command="pytest -q", ok=False, exit_code=1, timed_out=False, seconds=2, tail=log)
    check = FixCheck(passed=False, test_files=[], source_files=["a.py"], suite=[noisy], already_failing=["pytest -q"])
    text = prcheck.verification_summary(check)
    assert "Suite: `pytest -q` failed (exit 1)" in text and "1 failed, 8 passed" in text
    assert "log line" not in text and "Already failing on the base: pytest -q" in text


def test_tests_that_could_not_run_are_not_blamed_on_the_pr() -> None:
    def run(ok: bool, tail: str) -> CommandResult:
        return CommandResult(command="t", ok=ok, exit_code=0 if ok else 1, timed_out=False, seconds=1, tail=tail)

    skips = run(True, "3 skipped in 0.1s")
    skipped = FixCheck(passed=False, test_files=["t.py"], source_files=["a.py"], fails_without_fix=skips,
                       passes_with_fix=skips, problems=["the targeted test PASSES without the fix"])  # fmt: skip
    report = Report(pr=_pr())
    prcheck._findings(skipped, report, None)
    assert not report.must and "every test was skipped" in report.should[0]

    no_runner = "failed to load config from /work/repo/plugins/opencode/vitest.config.ts"
    broken = FixCheck(passed=False, test_files=["t.ts"], source_files=["a.ts"],
                      fails_without_fix=run(False, no_runner), passes_with_fix=run(False, no_runner),
                      problems=["the targeted test FAILS with the fix"])  # fmt: skip
    report = Report(pr=_pr())
    prcheck._findings(broken, report, None)
    assert not report.must and "could not run in this sandbox" in report.should[0]

    same = run(False, "E   assert 1 == 2\n1 failed")  # the fix did not work: still a result, not an environment limit
    real = FixCheck(passed=False, test_files=["t.py"], source_files=["a.py"], fails_without_fix=same,
                    passes_with_fix=same, problems=["the targeted test FAILS with the fix"])  # fmt: skip
    report = Report(pr=_pr())
    prcheck._findings(real, report, None)
    assert report.must == ["The targeted test FAILS with the fix."]  # a real failure stays a must-fix


def test_a_referenced_issue_is_not_a_closed_one() -> None:
    pr = _pr(body="Refs #4417. Partial fix.")
    assert prcheck.linked_issue_number(pr) is None and prcheck.referenced_issue_number(pr) == 4417


def test_what_a_pr_is_decides_whether_its_tests_must_fail_without_it() -> None:
    """pydantic benchmark: benchmark, refactor and test-only PRs were blocked for tests that pass both ways."""
    check = FixCheck(
        passed=False, test_files=["tests/t.py"], source_files=["a.py"],
        problems=["the targeted test PASSES without the fix, so it does not reproduce the issue",
                  "the issue's reproduction still fails with the PR, so the issue is not fixed",
                  "these passed before the change and fail now: pytest -q"],
    )  # fmt: skip
    report = Report(pr=_pr())
    prcheck._findings(check, report, None, intent="refactor")
    assert report.must == ["These passed before the change and fail now: pytest -q."]  # a regression still blocks
    assert "The PR's tests pass with and without its source changes, as a refactor should." in report.passed
    assert any(s.startswith("The checker's reproduction of the linked issue still fails") for s in report.should)
    fix = Report(pr=_pr())
    prcheck._findings(check.model_copy(update={"problems": check.problems[:1]}), fix, None, intent="fix")
    assert fix.must == ["The targeted test PASSES without the fix, so it does not reproduce the issue."]


def test_the_review_drops_the_title_link_and_cost() -> None:
    body = prcheck.review_body(Report(pr=_pr(), must=["Fix X."], cost_usd=0.43).markdown())
    assert body.startswith("**Needs work before review.**\n\n## Must fix")
    assert "https://github.com/o/r/pull/5" not in body and "$" not in body
    assert body.endswith(f"at {'h' * 7}._")


def test_a_check_that_could_not_run_is_never_ready() -> None:
    """The spend cap once stopped the plan and the review, and those checks read "Ready for review"."""
    report = Report(pr=_pr(), not_run=["The code review did not run: Daily budget reached."])
    assert report.verdict == "Not fully checked"
    assert "## Not checked\n\n- The code review did not run: Daily budget reached." in report.markdown()
    blocked = Report(pr=_pr(), must=["These passed before the change and fail now: pytest -q."],
                     not_run=["The code review did not run: timeout."])  # fmt: skip
    assert blocked.verdict == "Needs work before review"  # what was found still blocks
