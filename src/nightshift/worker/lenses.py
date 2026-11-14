"""Review lenses: focused checklists the Reviewer applies on top of its general review, chosen from what
the diff touches. At most two per review, so the prompt stays short and the review stays one model call.

The checklists are adapted from the reviewer agents in ECC (github.com/affaan-m/ecc, MIT licence):
security-reviewer, silent-failure-hunter, rag-pipeline-reviewer and pr-test-analyzer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_LENSES = 2


@dataclass(frozen=True)
class Lens:
    name: str
    title: str
    paths: re.Pattern[str] | None  # a changed file whose path matches
    added: re.Pattern[str] | None  # an added line that matches
    checklist: str


LENSES = [  # in priority order: the first MAX_LENSES that match are used
    Lens(
        "security",
        "Security",
        re.compile(r"auth|login|session|token|secret|password|crypt|permission|upload|sanitiz|webhook", re.I),
        re.compile(
            r"subprocess|shell=True|os\.system|\beval\(|\bexec\(|pickle\.loads?|yaml\.load\(|innerHTML"
            r"|dangerouslySetInnerHTML|child_process|\.execute\(\s*f?[\"'].*(%s|\{|\+)|verify=False|md5|sha1\(",
            re.I,
        ),
        "- Input from users, the network or files reaching a shell, SQL, eval, a template or HTML without "
        "escaping or parameters.\n"
        "- Secrets or tokens logged, returned in errors, or committed; credentials compared without a "
        "constant-time check.\n"
        "- Authorisation: every new endpoint or action checks who may do it; nothing trusts a client-sent id.\n"
        "- Unsafe deserialisation (pickle, yaml.load), disabled TLS verification, weak hashes for security.\n"
        "- Paths built from input without normalising (path traversal); uploads without type and size limits.",
    ),
    Lens(
        "silent_failures",
        "Silent failures",
        None,
        re.compile(
            r"^\s*(except\b|catch\b|\}\s*catch\b|rescue\b|recover\(\))|\.catch\(|except\s*:|except Exception"
            r"|\|\|\s*true\b|contextlib\.suppress|^\s*_\s*(,\s*_\s*)?=\s*\w+[.(]|\?\?\s|or \[\]|or \{\}",
            re.I,
        ),
        "- Errors caught and dropped: empty handlers, `pass`, `|| true`, a bare `except`, `.catch(() => [])`.\n"
        "- Fallbacks that hide a failure: returning an empty list, None or a default where the caller cannot "
        "tell it from a real result.\n"
        "- Lost context: re-raising without the cause, logging without what failed, the wrong log level.\n"
        "- Errors in async code, goroutines or callbacks that nobody awaits or checks.",
    ),
    Lens(
        "retrieval_eval",
        "Retrieval and evaluation",
        re.compile(r"retriev|rerank|embed|vector|rag\b|eval|metric|benchmark", re.I),
        re.compile(r"recall|precision|rerank|embedding|cosine|top_?k|ndcg|mrr|faithfulness", re.I),
        "- Metrics computed as their names say: recall over the labelled relevant items, not per chunk or "
        "per duplicate; precision, MRR and nDCG at the stated k.\n"
        "- The evaluated path is the production path: the same retriever, reranker and filters, not a stand-in.\n"
        "- Missing or partial data fails or is reported, never silently dropped from the score.\n"
        "- Gates have a versioned dataset, a baseline and stated thresholds; a change in the metric's "
        "definition moves the baseline with it.",
    ),
    Lens(
        "tests",
        "Test quality",
        re.compile(r"(^|/)(tests?|spec|__tests__)/|_test\.|\.test\.|\.spec\.|test_[^/]*$", re.I),
        None,
        "- Each changed behaviour has a test that would fail without the change: assertions on results, "
        "not only that nothing raised.\n"
        "- Edge cases and error paths of the change are covered, not just the happy path.\n"
        "- No flaky patterns: sleeps, real network, wall-clock time, order-dependent shared state.\n"
        "- Tests were not weakened to pass: removed assertions, widened tolerances, new skips.",
    ),
]


def changed_paths(diff: str) -> list[str]:
    return [line[6:] for line in diff.splitlines() if line.startswith("+++ b/")]


def added_lines(diff: str) -> list[str]:
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def pick(diff: str) -> list[Lens]:
    paths, added = changed_paths(diff), added_lines(diff)
    chosen = []
    for lens in LENSES:
        by_path = lens.paths is not None and any(lens.paths.search(p) for p in paths)
        by_line = lens.added is not None and any(lens.added.search(line) for line in added)
        if by_path or by_line:
            chosen.append(lens)
    return chosen[:MAX_LENSES]


def checklists(lenses: list[Lens]) -> str:
    """The lenses as one prompt section; empty when none apply."""
    return "\n\n".join(f"{lens.title}:\n{lens.checklist}" for lens in lenses)
