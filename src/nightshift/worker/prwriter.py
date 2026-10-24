"""The PR Writer: drafts the pull request for a verified fix, in the target repo's own conventions.

The model writes the title, the summary and the commit message, following the repo's PR template,
its rules and the titles of recently merged PRs. The evidence section is NOT written by the model:
code builds it from the verification that actually ran, so the PR can only claim what was proven.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from nightshift.config import get_settings
from nightshift.health.agents import UNTRUSTED_RULE
from nightshift.llm import LLMGateway
from nightshift.worker.verify import FixCheck

DIFF_CHARS = 30_000
TAIL_LINES = 12


class PRDraft(BaseModel):
    title: str = Field(
        description="PR title in the style of the repo's recent merged PRs (e.g. a conventional-commit prefix if they use one)"
    )
    summary: str = Field(
        description="Markdown body above the testing section: what was wrong, what changed and why, and anything a reviewer should check. If the repo has a PR template, fill its sections (except testing/evidence, which is added separately)."
    )
    commit_message: str = Field(
        description="Commit subject line, then a blank line and a short body, in the repo's commit style"
    )


PR_WRITER_SYSTEM = f"""You are the PR Writer for NightShift. You write the pull request a developer will submit to an open-source project for a fix that is already written, tested and reviewed.

Write like a considerate contributor: short, specific and plain. Explain the problem, the change and the reasons, and point the reviewer to anything worth a close look (e.g. a design choice the issue left open). Follow the repo's PR template and rules and the style of its recent PR titles. Do not claim anything the diff and the verification do not show. Do not add a testing or evidence section: code appends one from the real test runs. Do not mention NightShift or AI tools in the title or summary.

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
    )


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[-TAIL_LINES:])


def evidence_section(check: FixCheck) -> str:
    """The testing section, built only from what ran. Output is fenced so it renders as plain text."""
    out = ["## Testing", ""]
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


def pr_body(draft: PRDraft, check: FixCheck, issue_number: int) -> str:
    body = draft.summary.strip()
    closes = f"Closes #{issue_number}"
    if closes.lower() not in body.lower() and f"fixes #{issue_number}" not in body.lower():
        body = f"{closes}\n\n{body}"
    return f"{body}\n\n{evidence_section(check)}\n"


def commit_message(draft: PRDraft) -> str:
    attribution = get_settings().commit_attribution
    message = draft.commit_message.strip()
    return f"{message}\n\n{attribution}\n" if attribution else message + "\n"
