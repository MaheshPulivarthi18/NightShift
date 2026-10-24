"""The Reviewer: an independent check of a verified fix, before the operator sees it.

It never sees the Coder's reasoning, only what a maintainer would: the issue, the approved plan, the
diff, the verification results, the repo's rules, and the L3 change context, whose "related code NOT
changed" list it must account for item by item. Those are the paths a fix most often misses.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from nightshift.config import get_settings
from nightshift.health.agents import UNTRUSTED_RULE
from nightshift.llm import LLMGateway
from nightshift.router import RouteHint

DIFF_CHARS = 60_000


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
        description="Does the new test check the issue's behaviour, not an incidental detail?"
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
) -> Review:
    """`avoid`: the model family that wrote the code; another family reviews it when the ladder allows.
    `prefer`: the model the operator chose at Gate 1."""
    clipped = diff if len(diff) <= DIFF_CHARS else diff[:DIFF_CHARS] + "\n[diff cut here]"
    user = (
        f"Repository: {repo}\n\n<untrusted_github_data>\n<issue>\n{issue}\n</issue>\n</untrusted_github_data>\n\n"
        f"<approved_plan>\n{plan}\n</approved_plan>\n\n<repo_rules>\n{rules}\n</repo_rules>\n\n"
        f"<verification>\n{verification}\n</verification>\n\n<diff>\n{clipped}\n</diff>\n\n"
        f"<change_context>\n{change_context}\n</change_context>"
    )
    return await llm.parse(
        agent="reviewer",
        cfg=get_settings().reviewer,
        system=REVIEWER_SYSTEM,
        user=user,
        schema=Review,
        cache_key_parts=(),
        label="review",
        route=RouteHint(avoid=avoid, prefer=prefer),
    )


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
