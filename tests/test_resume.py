"""Revising a task picks up where the last attempt stopped: its change is applied, its baseline reused, and the
Coder gets only what failed. "Start over" begins from the plan. Every attempt is kept.
Uses the local Postgres (docker compose up); the sandbox and models are fakes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete

from nightshift.config import get_settings
from nightshift.db import Repo, Task, session_scope
from nightshift.worker import reviewer
from nightshift.worker import task as worker
from nightshift.worker.coder import CoderRun
from nightshift.worker.verify import CommandResult, FixCheck, step

OK = CommandResult(command="pytest", ok=True, exit_code=0, timed_out=False, seconds=300, tail="")
ISSUE = {"number": 7, "title": "add", "body": "", "state": "open", "author": "o", "labels": [], "assignees": [],
         "comments": [], "linked_prs": [], "url": "https://github.com/o/r/issues/7"}  # fmt: skip
PLAN = {"markdown": "# Plan", "setup_commands": [], "test_commands": ["pytest"], "size": "small",
        "files_to_change": [{"path": "calc.py", "why": "the bug"}],
        "test_plan": {"targeted_command": "pytest tests/test_calc.py"}}  # fmt: skip
DIFF = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-    return a - b\n+    return a + b\n"
REVIEW = {"round": 2, "verdict": "changes_needed", "summary": "Close, but the float path is missed.",
          "solves_issue": False, "test_reproduces_issue": True, "related": [],
          "issues": [{"severity": "major", "path": "calc.py", "line": 9, "problem": "add_floats still subtracts.",
                      "suggestion": "Apply the same fix to add_floats."}], "markdown": "..."}  # fmt: skip


@pytest.fixture
async def stopped_at_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[int]:
    monkeypatch.setattr(get_settings(), "reports_dir", str(tmp_path))
    name = f"test-resume/{uuid.uuid4().hex[:8]}"
    checks = [step("test", True, 2, 3, "ok"), step("review", False, 2, 40, "float path missed")]
    result = {"baseline": [OK.model_dump()], "reviews": [REVIEW], "checks": checks}
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name)
            s.add(repo)
            await s.flush()
            t = Task(repo_id=repo.id, issue_number=7, status="needs_operator", base_commit="abc", plan=PLAN,
                     result=result, issue=ISSUE, diff=DIFF, cost_usd=0.4,
                     error="the review still asks for changes after the last round")  # fmt: skip
            s.add(t)
            await s.flush()
            task_id = t.id
    except OSError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    yield task_id
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_continue_keeps_the_change_and_what_failed(stopped_at_review: int, tmp_path: Path) -> None:
    await worker.revise(stopped_at_review, "Also cover add_floats.")
    task, _ = await worker.load_task(stopped_at_review)
    assert task.status == "approved" and task.diff is None
    resume = task.result["resume"]
    assert resume["diff"] == DIFF and resume["stopped_at"] == "review" and resume["base"] == "abc"
    assert "add_floats still subtracts." in resume["todo"]  # the review's own words, for the Coder
    [attempt] = task.result["attempts"]
    assert attempt["stopped_at"] == "review" and attempt["cost_usd"] == 0.4
    patches = list(tmp_path.rglob("attempt-1.patch"))
    assert len(patches) == 1 and patches[0].read_text() == DIFF


async def test_start_over_keeps_the_history_but_not_the_change(stopped_at_review: int) -> None:
    await worker.revise(stopped_at_review, "Different approach.", mode="restart")
    task, _ = await worker.load_task(stopped_at_review)
    assert "resume" not in task.result and len(task.result["attempts"]) == 1


async def test_the_next_run_applies_the_change_and_skips_the_baseline(
    stopped_at_review: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    await worker.revise(stopped_at_review, "Also cover add_floats.")
    ran: list[str] = []
    instructions: list[str] = []

    class Box:
        async def write_file(self, path: str, content: str) -> None:
            ran.append(f"write {path}")

        async def exec(self, command: str, timeout: float | None = None, **kw: Any) -> Any:
            ran.append(command)
            return type("R", (), {"ok": True, "tail": lambda self, n=0: ""})()

        async def remove(self) -> None:
            pass

    async def prepare_repo(*args: Any, **kwargs: Any) -> tuple[Box, bool]:
        return Box(), True

    async def run_commands(*args: Any, **kwargs: Any) -> list[CommandResult]:
        raise AssertionError("the baseline must not run again on the same commit")

    async def work(self: CoderRun, instruction: str, deadline: float) -> None:
        instructions.append(instruction)
        self.check = FixCheck(passed=True, test_files=["tests/test_calc.py"], source_files=["calc.py"])

    async def review(llm: Any, **kwargs: Any) -> reviewer.Review:
        return reviewer.Review(verdict="approve", summary="Good.", solves_issue=True, test_reproduces_issue=True,
                               related=[], issues=[])  # fmt: skip

    async def check_suites(check: FixCheck, *args: Any) -> None:
        check.suites_run, check.passed = True, True

    async def nothing(*args: Any, **kwargs: Any) -> Any:
        return None

    async def no_diff(*args: Any, **kwargs: Any) -> str:
        return DIFF

    for name, fake in [("prepare_repo", prepare_repo), ("ensure_env_image", nothing), ("run_commands", run_commands),
                       ("save_recipe", nothing), ("change_context", nothing), ("diff", no_diff),
                       ("draft_pr", nothing), ("check_suites", check_suites)]:  # fmt: skip
        monkeypatch.setattr(worker, name, fake)
    monkeypatch.setattr(worker.archmap, "load_map", nothing)
    monkeypatch.setattr(worker, "change_markdown", lambda *a, **k: ("", 0))
    monkeypatch.setattr(worker.reviewer, "review", review)
    monkeypatch.setattr(CoderRun, "work", work)

    done = await worker.run_task(stopped_at_review)
    assert done.status == "ready"
    assert any(c.startswith("git apply --3way") for c in ran)  # the last attempt's change went back in
    assert instructions[0].startswith("Your previous attempt is already applied")
    assert "add_floats still subtracts." in instructions[0]
    assert "Fix the issue following the approved plan" not in instructions[0]
    assert "resume" not in done.result and done.result["baseline"][0]["seconds"] == 300  # reused, not rerun
