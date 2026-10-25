"""Gate 1 and run guards: a plan that cannot be verified or is not actionable never reaches the Coder."""

from __future__ import annotations

import time
from typing import Any

from nightshift.llm import Block, Turn
from nightshift.worker.coder import CoderRun
from nightshift.worker.task import run_blocker

PLAN = {"actionable": True, "test_commands": ["pytest -q"]}


def test_a_verifiable_actionable_plan_can_run() -> None:
    assert run_blocker(PLAN) is None
    assert run_blocker({"test_commands": ["pytest -q"]}) is None  # plans saved before `actionable` existed


def test_a_plan_that_is_not_actionable_cannot_run() -> None:
    why = run_blocker({**PLAN, "actionable": False})
    assert why and "not actionable" in why


def test_a_plan_without_a_test_command_cannot_run() -> None:
    why = run_blocker({**PLAN, "test_commands": []})
    assert why and "no test command" in why


class _Talker:
    """An LLM that only ever answers in text, never with a tool call."""

    async def create(self, **_: Any) -> Turn:
        return Turn(
            content=[Block(type="text", text="The plan says to ask the maintainers first.")], stop_reason="end_turn"
        )


async def test_a_coder_that_stops_says_why() -> None:
    coder = CoderRun(None, _Talker(), "base", [], [], "")  # type: ignore[arg-type]
    await coder.work("Fix it.", time.monotonic() + 60)
    assert coder.stopped == (
        "the Coder stopped without submitting. It said: The plan says to ask the maintainers first."
    )
    assert coder.steps == 2


def test_the_developers_note_heads_the_plan() -> None:
    from nightshift.worker.planner import FileChange, Plan, TestPlan, to_markdown

    plan = Plan(
        understanding="Docs for headers.", actionable=True, questions=[], approach=["Add a README section"],
        files_to_change=[FileChange(path="README.md", why="docs")],
        test_plan=TestPlan(file="", asserts="", targeted_command=""), setup_commands=[], test_commands=["pytest -q"],
        risks=[], size="small", claim_comment="",
    )  # fmt: skip
    text = to_markdown(plan, title="o/r#1 Docs", warnings=[], note="Treat this as a docs task.\nKeep it short.")
    assert "**Developer's note to the Planner:**\n\n> Treat this as a docs task.\n> Keep it short." in text
    assert "Developer's note" not in to_markdown(plan, title="o/r#1 Docs", warnings=[])
    assert "Docs change" not in text  # kind defaults to code
    assert "**Docs change.**" in to_markdown(plan.model_copy(update={"kind": "docs"}), title="t", warnings=[])


def test_gate_1_says_whether_the_reproduction_showed_the_bug() -> None:
    from nightshift.worker.task import reproduction_note
    from nightshift.worker.verify import CommandResult

    def run(ok: bool) -> CommandResult:
        return CommandResult(command="python r.py", ok=ok, exit_code=0 if ok else 1, timed_out=False, seconds=1,
                             tail="AssertionError: 0.5 != 0.75")  # fmt: skip

    checks, warnings = reproduction_note(run(False))
    assert checks and "fails on the current code" in checks[0] and not warnings
    checks, warnings = reproduction_note(run(True))
    assert warnings and "does not show the bug" in warnings[0] and not checks
    assert reproduction_note(None) == ([], [])
