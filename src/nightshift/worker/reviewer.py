"""The Reviewer: an independent check of a verified fix, before the operator sees it.

It never sees the Coder's reasoning, only what a maintainer would: the issue, the approved plan, the
diff, the verification results, the repo's rules, and the L3 change context, whose "related code NOT
changed" list it must account for item by item. Those are the paths a fix most often misses.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from nightshift import prompt
from nightshift.config import get_settings
from nightshift.health.agents import UNTRUSTED_RULE
from nightshift.llm import LLMGateway
from nightshift.prompt import Prompt
from nightshift.router import RouteHint


class ReviewIssue(BaseModel):
    severity: Literal["blocker", "major", "minor"] = Field(
        description="blocker: wrong or incomplete fix; major: a maintainer would ask for changes; minor: optional polish"
    )
    path: str
    line: int | None = Field(description="Line in the new file, if it applies")
    problem: str
    suggestion: str


class RelatedCheck(BaseModel):
    location: str = Field(description="The path:line from the 'related code NOT changed' list")
    needs_change: bool
    reason: str = Field(description="Why this code is or is not affected, citing what it does")


class Review(BaseModel):
    verdict: Literal["approve", "changes_needed"]
    summary: str = Field(description="Two or three sentences a maintainer would write")
    solves_issue: bool = Field(description="Does the change fully solve what the issue asks, including its comments?")
    test_reproduces_issue: bool = Field(
        description="Does the new test check the issue's behaviour, not an incidental detail? For a docs change, which has no test: true"
    )
    related: list[RelatedCheck] = Field(description="One entry per item in the 'related code NOT changed' list")
    issues: list[ReviewIssue]


REVIEWER_SYSTEM = f"""You are the Reviewer for NightShift. You review one pull request before a developer submits it to an open-source project, as that project's most careful maintainer would. You did not write it.

You get the issue, the plan the developer approved, the diff, the verification run by code (the new test fails without the fix and passes with it; suites compared with a baseline), the repository's rules, and the change context: the code around the change, its callers, and related code the diff did NOT change.

Check:
- Does it solve the issue completely, as the issue and its comments describe it? Trace every code path that produces the affected behaviour: a second endpoint, the streaming and non-streaming paths, error and fallback branches that build the same data.
- For EACH item under "related code NOT changed", decide whether it needs the same change, and say why from what the code does. An error or fallback path that builds the same response shape usually does.
- Does the test check the issue's behaviour, and is it written like the repo's tests?
- Scope: no unrelated changes, no weakened tests, no new dependencies, the repo's rules and style followed.

For a docs change (the verification says so), there is no test to judge. Check instead that every statement and code example is true of the code under <code_the_docs_describe>: names, signatures, defaults, behaviour. A claim the code does not support, or an example that would not run, is a major issue. Also check the text fits where it is placed and the repo's docs style.

Verdict changes_needed if there is any blocker or major issue; otherwise approve. Report minor issues without blocking. Be specific: path, line, problem, and the fix.

{UNTRUSTED_RULE}"""


async def review(
    llm: LLMGateway,
    *,
    repo: str,
    issue: str,
    plan: str,
    diff: str,
    verification: str,
    rules: str,
    change_context: str,
    avoid: str | None = None,
    prefer: str | None = None,
    sources: str = "",
) -> Review:
    """`avoid`: the model family that wrote the code; another family reviews it when the ladder allows.
    `prefer`: the model the operator chose at Gate 1."""
    return await llm.parse(
        agent="reviewer",
        cfg=get_settings().reviewer,
        system=REVIEWER_SYSTEM,
        user=review_prompt(
            repo=repo,
            issue=issue,
            plan=plan,
            rules=rules,
            verification=verification,
            diff=diff,
            change_context=change_context,
            sources=sources,
        ),  # fmt: skip
        schema=Review,
        cache_key_parts=(),
        label="review",
        route=RouteHint(avoid=avoid, prefer=prefer),
    )


def review_prompt(
    *,
    repo: str,
    issue: str,
    plan: str,
    rules: str,
    verification: str,
    diff: str,
    change_context: str,
    sources: str = "",
) -> Prompt:
    """The repo's rules, then the issue and the approved plan, which stay the same across review rounds,
    then what each round changes: so rounds two and three read the start of the prompt from the cache."""
    p = Prompt()
    p.add("repo", "repository", repo, 50)
    p.add("repo", "repo_rules", rules, prompt.BRIEF)
    p.add("item", "untrusted_github_data", f"<issue>\n{issue}\n</issue>", prompt.ITEM)
    p.add("item", "approved_plan", plan, prompt.ITEM)
    p.add("task", "verification", verification, prompt.ENVIRONMENT * 2)
    p.add("task", "diff", diff, prompt.DIFF)
    p.add("task", "change_context", change_context, prompt.ITEM)
    p.add("task", "code_the_docs_describe", sources, prompt.CODE)
    return p


def blocking(r: Review) -> list[ReviewIssue]:
    return [i for i in r.issues if i.severity in ("blocker", "major")]


def to_markdown(r: Review, round_no: int) -> str:
    out = [f"## Review round {round_no}: {r.verdict}", "", r.summary, ""]
    out.append(f"- Solves the issue: {'yes' if r.solves_issue else 'no'}")
    out.append(f"- Test reproduces the issue: {'yes' if r.test_reproduces_issue else 'no'}")
    if r.related:
        out += ["", "Related code not changed by the diff:"]
        out += [f"- `{c.location}`: {'NEEDS CHANGE' if c.needs_change else 'fine'}. {c.reason}" for c in r.related]
    if r.issues:
        out += ["", "Issues:"]
        for i in r.issues:
            where = f"{i.path}:{i.line}" if i.line else i.path
            out.append(f"- **{i.severity}** `{where}`: {i.problem} Fix: {i.suggestion}")
    return "\n".join(out)


def feedback_for_coder(r: Review) -> str:
    """The review as an instruction to the Coder for the next round."""
    lines = ["An independent review asked for changes. Address every item below, then submit again.", ""]
    for c in r.related:
        if c.needs_change:
            lines.append(f"- `{c.location}` needs the same change: {c.reason}")
    for i in blocking(r):
        where = f"{i.path}:{i.line}" if i.line else i.path
        lines.append(f"- [{i.severity}] `{where}`: {i.problem} Suggested fix: {i.suggestion}")
    return "\n".join(lines)
