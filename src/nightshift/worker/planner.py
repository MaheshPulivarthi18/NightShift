"""The Planner: reads the issue and the repo's context, and proposes how to fix it. Stops at Gate 1.

The plan is what the operator approves before anything costs real money or touches GitHub: the
approach, the files, the test that will reproduce the issue, the commands that set the repo up and
test it, and a draft claim comment. Code then checks every path the plan names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from nightshift.config import get_settings
from nightshift.health.agents import UNTRUSTED_RULE
from nightshift.llm import LLMGateway

# What a task of each size is expected to cost on the default models (USD). Set by code, not the model.
SIZE_COST = {"small": 1.0, "medium": 2.5, "large": 5.0}


class FileChange(BaseModel):
    path: str = Field(description="Repo-relative path; an existing file, or a new test file")
    why: str


class TestPlan(BaseModel):
    file: str = Field(description="Test file to add or extend, following the repo's existing test layout")
    asserts: str = Field(description="The behaviour the test checks, which fails today because of the issue")
    targeted_command: str = Field(
        description="Command that runs only this test from the repo root, e.g. `cd engine && .venv/bin/pytest tests/test_x.py -q`"
    )


class Plan(BaseModel):
    understanding: str = Field(description="What is wrong or wanted, in your own words, two to four sentences")
    actionable: bool = Field(
        description="False if the issue is unclear, needs a maintainer decision, or is not a code change"
    )
    questions: list[str] = Field(description="What must be answered before working; empty if actionable")
    kind: Literal["code", "docs"] = Field(
        default="code",
        description="docs: the change only touches documentation files (README, docs/, other prose files), "
        "so there is no failing-first test; code: anything else, docstrings and comments included",
    )
    approach: list[str] = Field(description="Ordered steps of the fix")
    files_to_change: list[FileChange]
    test_plan: TestPlan
    setup_commands: list[str] = Field(
        description="Commands run from the repo root in a fresh Linux container (Python 3.12, Node 22, Bun, uv, git, a C/C++ compiler; no sudo) to install what the tests need"
    )
    test_commands: list[str] = Field(description="Commands that run the full relevant test suites from the repo root")
    risks: list[str] = Field(description="What could break or be missed, e.g. other code paths that must change too")
    size: Literal["small", "medium", "large"]
    claim_comment: str = Field(
        description="Short, friendly comment to post on the issue saying the operator will work on it and how, "
        "in plain words. If not actionable, it asks the questions instead"
    )


PLANNER_SYSTEM = f"""You are the Planner for NightShift, which helps one developer contribute fixes to open-source projects. You plan how to fix one GitHub issue. A separate coding agent will follow your plan inside a sandbox, and the developer approves your plan first.

You get the issue with its comments, the repository's contribution rules and commands (repo brief), an architecture map, the files that mention the issue's terms (with excerpts), the sandbox environment the Setup agent found for this repo, and possibly setup and test commands that worked before.

- Understand the actual problem before proposing a fix. Read the end of long issue bodies and the comments: the requested behaviour is often refined there.
- Plan the smallest change that fully solves it, following the repo's existing patterns and rules.
- Think about every code path the change affects: other callers, a second endpoint that builds the same response, the streaming and non-streaming versions of the same flow. List the ones that must change in files_to_change and the doubtful ones in risks.
- The test must reproduce the issue: it fails on the current code and passes once fixed. Put it where the repo's existing tests for that area live. targeted_command runs only that test.
- Setup and test commands run in a fresh Linux container from the repo root, with no services running and no secrets. If <sandbox_environment> is ready, its setup commands were run in that container and work: use them as they are, adding only what the fix needs, and pick test_commands from its test commands, scoped to the change. A test command that fails there already fails on the default branch. Otherwise prefer the commands in the repo's documents or the ones that worked before. Lock files over loose requirements. Scope test_commands to the parts of the repo the change touches.
- The container has Python 3.12, Node 22, Bun, uv, git and a C/C++ compiler, plus the system packages and tools in <sandbox_environment>, and no sudo. If <setup_failed> is present, your earlier setup commands were run there and failed with that output: propose commands that work in that container.
- If the issue is unclear or needs a maintainer's decision first, set actionable to false and ask in questions.
- <developer_note>, when present, is the developer's own instruction, written in NightShift (not on GitHub): follow it. It sets the scope, e.g. "treat this as a docs task" makes a question issue actionable as that docs change. Set actionable to false only if the note's task itself cannot be done from the repo.
- kind docs is for a change to documentation files only. It has no reproducing test: leave the test_plan's file and targeted_command empty, keep test_commands as a sanity run of the suites, and plan to check every statement and example against the code that implements it.
- Use only paths that appear in the input or follow the repo's layout for a new test file.

{UNTRUSTED_RULE}"""


async def make_plan(llm: LLMGateway, *, repo: str, context: str) -> Plan:
    return await llm.parse(
        agent="planner",
        cfg=get_settings().planner,
        system=PLANNER_SYSTEM,
        user=f"Repository: {repo}\n\n{context}",
        schema=Plan,
        cache_key_parts=(),
        label="plan",
    )


def unknown_paths(plan: Plan, tracked: set[str]) -> list[str]:
    """Paths the plan names that do not exist, other than the new test file it proposes."""
    named = [f.path for f in plan.files_to_change]
    return [p for p in named if p not in tracked and p != plan.test_plan.file]


def to_markdown(plan: Plan, *, title: str, warnings: list[str], checks: list[str] | None = None, note: str = "") -> str:
    out = [f"# Plan: {title}", ""]
    if note:
        out += ["**Developer's note to the Planner:**", "", *[f"> {line}" for line in note.splitlines()], ""]
    out += [plan.understanding, ""]
    if plan.kind == "docs":
        out += [
            "**Docs change.** No failing-first test: verification checks that only documentation files "
            "change and the suites still pass; the Reviewer checks every claim against the code.",
            "",
        ]
    if checks:
        out += ["**Checked by code:**", *[f"- {c}" for c in checks], ""]
    if warnings:
        out += ["**Warnings (checked by code):**", *[f"- {w}" for w in warnings], ""]
    if not plan.actionable:
        out += ["**Not actionable yet.** Questions:", *[f"- {q}" for q in plan.questions], ""]
    out += ["## Approach", *[f"{i}. {s}" for i, s in enumerate(plan.approach, 1)], ""]
    out += ["## Files", *[f"- `{f.path}`: {f.why}" for f in plan.files_to_change], ""]
    t = plan.test_plan
    if plan.kind == "docs" and not t.targeted_command:
        out += ["## Test", "- None: a docs change has no failing-first test (see above).", ""]
    else:
        out += ["## Test", f"- File: `{t.file}`", f"- Checks: {t.asserts}", f"- Run: `{t.targeted_command}`", ""]
    out += ["## Commands", "Setup:", *[f"- `{c}`" for c in plan.setup_commands], "Tests:"]
    out += [f"- `{c}`" for c in plan.test_commands]
    out += ["", "## Risks", *[f"- {r}" for r in plan.risks]]
    out += ["", f"## Size: {plan.size} (expected cost about ${SIZE_COST[plan.size]:.2f})"]
    out += ["", "## Claim comment (draft, not posted)", "", plan.claim_comment]
    return "\n".join(out)
