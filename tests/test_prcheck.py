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
