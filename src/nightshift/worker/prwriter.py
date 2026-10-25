"""The PR Writer: drafts the pull request for a verified fix, in the target repo's own conventions.

The model writes the title, the summary and the commit message, following the repo's PR template,
its rules and the titles of recently merged PRs. The evidence section is NOT written by the model:
code builds it from the verification that actually ran, so the PR can only claim what was proven.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from nightshift.config import get_settings
from nightshift.health.agents import UNTRUSTED_RULE
from nightshift.llm import LLMGateway
from nightshift.router import RouteHint
from nightshift.worker.verify import FixCheck

DIFF_CHARS = 30_000
TAIL_LINES = 12


class PRDraft(BaseModel):
    title: str = Field(
        description="PR title in the style of the repo's recent merged PRs (e.g. a conventional-commit prefix if they use one)"
    )
    summary: str = Field(
        description="Markdown body: what was wrong, what changed and why, and anything a reviewer should check. With a PR template: every one of its headings, in its order, each filled in. Where the template's testing or test-output section goes, write only the line <!-- testing --> and code puts the real test runs there."
    )
    commit_message: str = Field(
        description="Commit subject line, then a blank line and a short body, in the repo's commit style"
    )


PR_WRITER_SYSTEM = f"""You are the PR Writer for NightShift. You write the pull request a developer will submit to an open-source project for a fix that is already written, tested and reviewed.

Write like a considerate contributor: short, specific and plain. Explain the problem, the change and the reasons, and point the reviewer to anything worth a close look (e.g. a design choice the issue left open). Follow the repo's rules and the style of its recent PR titles. Do not claim anything the diff and the verification do not show. Do not mention NightShift or AI tools in the title or summary.

If the repo has a PR template, the summary keeps every heading of it, in order, and fills each section:
- Checkboxes: tick [x] only what the diff or the verification shows (e.g. "Bug fix", "Tests added", "Unit tests pass" when the suites passed). Leave the rest [ ] and say why in that section when it matters (e.g. type checking was not run).
- A section you cannot fill from what you have (e.g. a real-run proof, screenshots, a rollout plan) gets a short honest line saying what is missing; never invent it.
- The testing or test-output section: put the line <!-- testing --> where the test runs go. Code fills it from the runs that actually happened.
Without a template, do not write a testing section: code appends one.

{UNTRUSTED_RULE}"""


async def draft_pr(
    llm: LLMGateway,
    *,
    repo: str,
    issue: str,
    diff: str,
    review: str,
    rules: str,
    template: str,
    recent_titles: list[str],
    prefer: str | None = None,
) -> PRDraft:
    clipped = diff if len(diff) <= DIFF_CHARS else diff[:DIFF_CHARS] + "\n[diff cut here]"
    user = (
        f"Repository: {repo}\n\n<untrusted_github_data>\n<issue>\n{issue}\n</issue>\n"
        f"<pr_template>\n{template or '(none)'}\n</pr_template>\n"
        f"<recent_merged_pr_titles>\n" + "\n".join(recent_titles) + "\n</recent_merged_pr_titles>\n"
        f"</untrusted_github_data>\n\n<repo_rules>\n{rules}\n</repo_rules>\n\n"
        f"<review>\n{review}\n</review>\n\n<diff>\n{clipped}\n</diff>"
    )
    return await llm.parse(
        agent="pr_writer",
        cfg=get_settings().pr_writer,
        system=PR_WRITER_SYSTEM,
        user=user,
        schema=PRDraft,
        cache_key_parts=(),
        label="PR draft",
        route=RouteHint(prefer=prefer),
    )


# Lines worth quoting from a test run: the runner's result line and the failing assertion. Everything
# else (progress dots, log output, warnings) is noise in a PR.
_RESULT = re.compile(
    r"\b\d+ (passed|failed|errors?|skipped)\b|^Ran \d+ tests?|^(OK|FAILED)\b|^Tests?:\s|^Test Suites:|^(ok|FAIL)\s+\S+"
    r"|\b\d+ (passing|failing)\b",
    re.I,
)
_FAILURE = re.compile(r"^E\s+|AssertionError|assert |Error:|^FAILED |expected|received|panic:", re.I)
ESSENTIAL_LINES = 8


def _tail(text: str) -> str:
    """The test runner's summary and the failing assertion; the last lines when neither is found."""
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    failures = [line for line in lines if _FAILURE.search(line)][:4]
    results = [line for line in lines if _RESULT.search(line)][-2:]
    picked = [line for line in lines if line in failures or line in results]
    return "\n".join((picked or lines[-TAIL_LINES:])[-ESSENTIAL_LINES:])


def evidence_section(check: FixCheck) -> str:
    """The testing section, built only from what ran. Output is fenced so it renders as plain text."""
    out = ["## Testing", ""]
    if check.kind == "docs":
        out += ["Documentation-only change (" + ", ".join(f"`{f}`" for f in check.source_files) + ").", ""]
    if check.fails_without_fix and check.passes_with_fix:
        cmd = check.fails_without_fix.command
        out += [
            f"New test in {', '.join(f'`{t}`' for t in check.test_files)}, run with `{cmd}`:",
            "",
            "- **Before the fix** (source changes removed): fails",
            "",
            "```",
            _tail(check.fails_without_fix.tail),
            "```",
            "",
            "- **After the fix:** passes",
            "",
            "```",
            _tail(check.passes_with_fix.tail),
            "```",
            "",
        ]
    if check.suite:
        out += ["Existing suites:", ""]
        out += [f"- `{r.command}`: {'passed' if r.ok else 'failed'}" for r in check.suite]
    if check.already_failing:
        out += ["", "Already failing before this change: " + ", ".join(f"`{c}`" for c in check.already_failing)]
    return "\n".join(out)


TESTING_MARK = "<!-- testing -->"


def pr_body(draft: PRDraft, check: FixCheck, issue_number: int) -> str:
    """The summary with the code-built evidence: where the template's testing section put the marker,
    else appended as its own section."""
    body = draft.summary.strip()
    closes = f"Closes #{issue_number}"
    if closes.lower() not in body.lower() and f"fixes #{issue_number}" not in body.lower():
        body = f"{closes}\n\n{body}"
    evidence = evidence_section(check)
    if TESTING_MARK in body:
        return body.replace(TESTING_MARK, evidence.removeprefix("## Testing").strip(), 1).rstrip() + "\n"
    return f"{body}\n\n{evidence}\n"


def commit_message(draft: PRDraft) -> str:
    """The commit message as written. The attribution line is added at Gate 2, where it is shown."""
    return draft.commit_message.strip() + "\n"
