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
