"""The plan check: an independent reviewer reads a plan against the live code before Gate 1.

It follows two principles of Google's Elephant-Goldfish method (github.com/google/elephant-goldfish-model,
Apache-2.0): a reviewer that never saw how the plan was made shares none of the planner's blind spots,
and a plan is checked against the real code before any code is written. The checker gets only the
issue, the plan and read-only access to the repository at the plan's commit, runs on another model
family than the Planner's, and answers two questions:

- Is there code that needs the same change but is not in the plan? (multilspy #101: the plan named the
  base class; six subclasses needed the same change.)
- Could a developer carry out each step without making a design decision of their own?

A must-fix finding sends the plan back to the Planner once; every finding is shown at Gate 1.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from nightshift import prompt
from nightshift.config import get_settings
from nightshift.health.agents import UNTRUSTED_RULE
from nightshift.llm import LLMGateway, ToolParam
from nightshift.prompt import Prompt
from nightshift.router import RouteHint
from nightshift.sandbox.docker import Sandbox
from nightshift.worker.coder import READ_TOOL_NAMES, TOOLS, ToolError, read_tool

MAX_STEPS = 12


class Finding(BaseModel):
    severity: Literal["must", "should"] = Field(
        description="must: the fix would be incomplete or wrong as planned; should: worth fixing in the plan"
    )
    kind: Literal["missing_change", "vague_step", "wrong_assumption"]
    location: str = Field(description="path:line in the code, or the plan step it is about")
    problem: str
    evidence: str = Field(description="What you read that shows it, e.g. the path:line of each subclass")


class PlanCheck(BaseModel):
    summary: str
    findings: list[Finding]

    def must(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "must"]


REPORT: ToolParam = {
    "name": "report",
    "description": "Report your findings and finish. Call it once, after reading the code.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "One or two sentences: is the plan ready to code?"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["must", "should"]},
                        "kind": {"type": "string", "enum": ["missing_change", "vague_step", "wrong_assumption"]},
                        "location": {"type": "string"},
                        "problem": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["severity", "kind", "location", "problem", "evidence"],
                },
            },
        },
        "required": ["summary", "findings"],
    },
}

SYSTEM = f"""You check a plan to fix a GitHub issue before anyone writes code. You did not write the plan and you do not know how it was made: judge it only against the issue and the repository's actual code, which you can read and search.

Answer two questions, reading the code to be sure:
1. Missing changes: is there code that needs the same change but is not in the plan's files? Look for other implementations of what the plan changes: subclasses or sibling classes overriding the same method, a second endpoint or handler building the same data, the streaming and non-streaming paths, other callers that depend on the changed behaviour. Search for the names the plan changes and read what you find.
2. Vague steps: could a developer carry out each step without making a design decision of their own (which type, which signature, where the code goes, how errors are handled)?
Also flag a plan built on an assumption the code contradicts (wrong_assumption).

Severity must: the fix would be incomplete or wrong as planned. should: worth fixing in the plan, but the fix would still work. Report only what you verified in the code, with the path:line as evidence. No findings is a valid answer for a good plan. Do not suggest unrelated improvements.

Use the tools to read and search, then call report once.

{UNTRUSTED_RULE}"""


def check_prompt(issue: str, plan: str) -> Prompt:
    p = Prompt()
    p.add("item", "untrusted_github_data", f"<issue>\n{issue}\n</issue>", prompt.ITEM)
    p.add("item", "plan", plan, prompt.ITEM)
    return p


async def check_plan(llm: LLMGateway, box: Sandbox, *, issue: str, plan: str, avoid: str | None) -> PlanCheck:
    """Read the plan against the code in `box` (read-only) and report what it misses.
    `avoid`: the Planner's model family; another one checks its plan when the ladder allows."""
    settings = get_settings()
    tools = [t for t in TOOLS if t["name"] in READ_TOOL_NAMES] + [REPORT]
    messages: list[dict[str, Any]] = [{"role": "user", "content": check_prompt(issue, plan).blocks()}]
    for step in range(1, MAX_STEPS + 1):
        last = step == MAX_STEPS
        if last:
            messages.append({"role": "user", "content": [{"type": "text", "text": "Call report now."}]})
        turn = await llm.create(
            agent="plan_checker",
            cfg=settings.plan_checker,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=[REPORT] if last else tools,
            label=f"step {step}",
            route=RouteHint(avoid=avoid),
        )
        messages.append(
            {"role": "assistant", "content": [b.model_dump(mode="json", exclude_none=True) for b in turn.content]}
        )
        uses = [b for b in turn.content if b.type == "tool_use"]
        if not uses:
            messages.append({"role": "user", "content": [{"type": "text", "text": "Use the tools, then call report."}]})
            continue
        results = []
        for block in uses:
            args = block.input if isinstance(block.input, dict) else {}
            if block.name == "report":
                try:
                    return PlanCheck.model_validate(args)
                except ValidationError as e:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Invalid report: {e}",
                            "is_error": True,
                        }
                    )
                    continue
            try:
                text, is_error = await read_tool(box, str(block.name), args), False
            except ToolError as e:
                text, is_error = str(e), True
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return PlanCheck(summary="The plan check did not finish within its step limit.", findings=[])


class Addressed(BaseModel):
    addressed: bool = Field(description="True if the revised plan fixes this finding")
    how: str = Field(description="One sentence: where the revised plan fixes it, or what is still missing")


class Recheck(BaseModel):
    findings: list[Addressed] = Field(description="One entry per finding, in the order given")


RECHECK_SYSTEM = """An independent check of a plan for fixing a GitHub issue found must-fix problems, and the Planner revised the plan. For each finding, say whether the revised plan addresses it: the plan now changes what the finding says it missed, or explains with a reason why the finding does not apply. A plan that only mentions the finding without changing anything does not address it. Judge from the plan text; do not invent code."""


async def recheck(llm: LLMGateway, findings: list[Finding], plan: str, avoid: str | None) -> list[Addressed]:
    """Whether the revised plan addresses each must-fix finding: one short call with no code reading, so the
    revised plan does not reach Gate 1 unchecked (it is not re-planned again: one revision is the cap)."""
    listed = "\n".join(f"<finding n={i}>\n{f.location}: {f.problem}\n</finding>" for i, f in enumerate(findings, 1))
    out = await llm.parse(
        agent="plan_checker", cfg=get_settings().plan_checker, system=RECHECK_SYSTEM,
        user=f"{listed}\n\n<revised_plan>\n{plan}\n</revised_plan>", schema=Recheck,
        cache_key_parts=("recheck",), label="re-check of the revised plan", route=RouteHint(avoid=avoid),
    )  # fmt: skip
    unanswered = [Addressed(addressed=False, how="The re-check gave no answer for this finding.")] * len(findings)
    return (out.findings + unanswered)[: len(findings)]


def as_note(findings: list[Finding]) -> str:
    """The findings as the Planner's input for its second plan."""
    return "\n".join(f"- [{f.severity}] {f.location}: {f.problem} (evidence: {f.evidence})" for f in findings)


def to_markdown(check: PlanCheck, revised: bool, rechecked: list[Addressed] | None = None) -> list[str]:
    out = ["## Independent plan check", "", check.summary]
    if revised:
        out += ["", "The plan was revised once to address the must-fix findings below."]
    if rechecked:
        done = sum(r.addressed for r in rechecked)
        out += [f"Re-checked after the revision: {done} of {len(rechecked)} addressed."]
        out += [f"- {'addressed' if r.addressed else '**not addressed**'}: {f.problem} {r.how}"
                for f, r in zip(check.must(), rechecked, strict=False)]  # fmt: skip
        out.append("")
    for f in check.findings:
        out.append(
            f"- **{f.severity}** ({f.kind.replace('_', ' ')}) `{f.location}`: {f.problem} Evidence: {f.evidence}"
        )
    if not check.findings:
        out.append("- No findings.")
    return out
