"""The plan check: an independent, read-only reviewer of a plan against the code."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fake_openrouter import FakeOpenRouter, reply
from test_llm_gateway import _gateway, repo_id  # noqa: F401  (fixture)

from nightshift.sandbox.docker import ExecResult
from nightshift.worker import plancheck

ISSUE = "start_server cannot be entered twice"
PLAN = "## Files\n- `src/base.py`: reset the readiness events in start_server"


class Box:
    """A sandbox stand-in that answers searches and records every command it was asked to run."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec(self, command: str, **_: Any) -> ExecResult:
        self.commands.append(command)
        out = "src/servers/gopls.py:12:class Gopls(LanguageServer):\nsrc/servers/clangd.py:9:class Clangd(Base):\n"
        return ExecResult(0, out, "", 0.1, False)


FINDINGS = {
    "summary": "Two subclasses override start_server and need the same reset.",
    "findings": [
        {"severity": "must", "kind": "missing_change", "location": "src/servers/gopls.py:12",
         "problem": "Gopls overrides start_server and keeps the old events.", "evidence": "src/servers/gopls.py:12"},
        {"severity": "should", "kind": "vague_step", "location": "plan step 1",
         "problem": "Which events, and where are they created?", "evidence": "src/base.py"},
    ],
}  # fmt: skip


async def test_the_checker_reads_the_code_then_reports(repo_id: int) -> None:  # noqa: F811
    fake = FakeOpenRouter(
        reply(tool_calls=[("c1", "search", {"pattern": "(LanguageServer)"})], finish="tool_calls"),
        reply(tool_calls=[("c2", "report", FINDINGS)], finish="tool_calls"),
    )
    box = Box()
    check = await plancheck.check_plan(_gateway(repo_id, fake), box, issue=ISSUE, plan=PLAN, avoid="anthropic")  # type: ignore[arg-type]
    assert [f.severity for f in check.findings] == ["must", "should"] and len(check.must()) == 1
    assert box.commands and "git grep" in box.commands[0]
    first = fake.bodies[0]
    assert {t["function"]["name"] for t in first["tools"]} == {"read_file", "list_files", "search", "report"}
    assert not first["model"].startswith("anthropic/")  # another family than the Planner's
    sent = str(first["messages"])
    assert ISSUE in sent and "reset the readiness events" in sent


async def test_a_checker_that_never_reports_ends_with_no_findings(
    repo_id: int,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plancheck, "MAX_STEPS", 2)
    fake = FakeOpenRouter(reply(tool_calls=[("c1", "search", {"pattern": uuid.uuid4().hex})], finish="tool_calls"))
    check = await plancheck.check_plan(_gateway(repo_id, fake), Box(), issue=ISSUE, plan=PLAN, avoid=None)  # type: ignore[arg-type]
    assert check.findings == [] and "did not finish" in check.summary
    assert [t["function"]["name"] for t in fake.bodies[-1]["tools"]] == ["report"]  # the last step may only report


def test_findings_go_to_the_planner_and_to_gate_1() -> None:
    check = plancheck.PlanCheck.model_validate(FINDINGS)
    note = plancheck.as_note(check.must())
    assert "src/servers/gopls.py:12" in note and "vague" not in note
    page = "\n".join(plancheck.to_markdown(check, revised=True))
    assert "revised once" in page and "**must** (missing change)" in page and "**should** (vague step)" in page
