"""Which review lenses a diff gets, and how they reach the Reviewer's prompt."""

from __future__ import annotations

from nightshift.worker.lenses import MAX_LENSES, pick
from nightshift.worker.reviewer import review_prompt


def diff(path: str, *added: str) -> str:
    lines = "\n".join(f"+{a}" for a in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{lines}\n"


def names(d: str) -> list[str]:
    return [lens.name for lens in pick(d)]


def test_shell_calls_get_the_security_lens() -> None:
    assert names(diff("app/run.py", "subprocess.run(cmd, shell=True)")) == ["security"]


def test_swallowed_errors_get_the_silent_failure_lens() -> None:
    assert "silent_failures" in names(diff("app/x.py", "try:", "    go()", "except Exception:", "    pass"))
    assert "silent_failures" in names(diff("web/a.ts", "fetchAll().catch(() => [])"))


def test_go_error_checks_alone_are_not_silent_failures() -> None:
    assert names(diff("pkg/a.go", "if err != nil {", "    return err", "}")) == []


def test_retrieval_metrics_get_the_retrieval_lens() -> None:
    assert names(diff("scripts/evaluate_retrieval.py", "recall = hits / k")) == ["retrieval_eval"]


def test_at_most_two_lenses_in_priority_order() -> None:
    d = diff("auth/eval_test.py", "except Exception:", "    pass", "recall = 1")
    assert names(d) == ["security", "silent_failures"]
    assert len(pick(d)) == MAX_LENSES


def test_plain_change_gets_none() -> None:
    assert names(diff("README.md", "A new sentence.")) == []


def prompt(d: str, untrusted: bool = False) -> str:
    p = review_prompt(repo="o/r", issue="i", plan="the plan", rules="", verification="v", diff=d,
                      change_context="", untrusted=untrusted)  # fmt: skip
    return str(p.blocks())


def test_lenses_reach_the_prompt() -> None:
    text = prompt(diff("app/run.py", "os.system(x)"))
    assert "<review_lenses>" in text and "Security:" in text
    assert "<review_lenses>" not in prompt(diff("README.md", "words"))


def test_a_checked_pr_is_untrusted_data() -> None:
    text = prompt(diff("a.py", "x = 1"), untrusted=True)
    assert "<pull_request_description>" in text and "<approved_plan>" not in text
    assert text.count("<untrusted_github_data>") == 3  # the issue, the description, the diff
    own = prompt(diff("a.py", "x = 1"))
    assert "<approved_plan>" in own and "<diff>" in own


def test_the_injection_rule_spares_ordinary_words_to_maintainers() -> None:
    from nightshift.worker.reviewer import REVIEWER_SYSTEM

    assert "aimed at an AI or automated reviewer" in REVIEWER_SYSTEM
    assert "happy to change this if you agree" in REVIEWER_SYSTEM
