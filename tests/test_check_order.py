"""The checks run cheapest first: the full suites run once, after the Reviewer approves, and a regression they
find goes back to the Coder. Uses the local Postgres (docker compose up); the sandbox and models are fakes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete

from nightshift.db import Repo, Task, session_scope
from nightshift.worker import reviewer
from nightshift.worker import task as worker
from nightshift.worker.coder import CoderRun
from nightshift.worker.verify import CommandResult, FixCheck, step

OK = CommandResult(command="pytest", ok=True, exit_code=0, timed_out=False, seconds=1, tail="")
ISSUE = {"number": 7, "title": "add", "body": "", "state": "open", "author": "o", "labels": [], "assignees": [],
         "comments": [], "linked_prs": [], "url": "https://github.com/o/r/issues/7"}  # fmt: skip
BROKEN = OK.model_copy(update={"ok": False, "exit_code": 1, "tail": "1 failed"})


class Box:
    async def exec(self, command: str, timeout: float | None = None) -> Any:
        raise AssertionError(f"nothing should run in the sandbox here: {command}")

    async def remove(self) -> None:
        pass


@pytest.fixture
async def approved() -> AsyncIterator[int]:
    name = f"test-order/{uuid.uuid4().hex[:8]}"
    plan = {"markdown": "# Plan", "setup_commands": [], "test_commands": ["pytest"], "size": "small",
            "files_to_change": [{"path": "calc.py", "why": "the bug"}],
            "test_plan": {"targeted_command": "pytest tests/test_calc.py"}}  # fmt: skip
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name)
            s.add(repo)
            await s.flush()
            t = Task(repo_id=repo.id, issue_number=7, status="approved", base_commit="abc", plan=plan, result={},
                     issue=ISSUE)  # fmt: skip
            s.add(t)
            await s.flush()
            task_id = t.id
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    yield task_id
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_suites_run_after_approval_and_a_regression_goes_back(
    approved: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    async def prepare_repo(*args: Any, **kwargs: Any) -> tuple[Box, bool]:
        return Box(), True

    async def run_commands(box: Any, commands: list[str], timeout: float | None = None) -> list[CommandResult]:
        order.append("baseline")
        return [OK]

    async def work(self: CoderRun, instruction: str, deadline: float) -> None:
        order.append("coder")
        self.check = FixCheck(passed=True, test_files=["tests/test_calc.py"], source_files=["calc.py"])
        self.checks.append(step("scope", True, self.round, 0, "12 of 400 lines"))
        self.checks.append(step("test", True, self.round, 2, "fails without the fix, passes with it"))

    async def review(llm: Any, **kwargs: Any) -> reviewer.Review:
        order.append("review")
        assert "suites" not in order[-2:]  # the Reviewer sees the change before the slow suites run
        return reviewer.Review(verdict="approve", summary="Looks right.", solves_issue=True,
                               test_reproduces_issue=True, related=[], issues=[])  # fmt: skip

    async def check_suites(check: FixCheck, box: Any, suite: list[str], baseline: list[CommandResult]) -> None:
        order.append("suites")
        first = order.count("suites") == 1
        check.suites_run, check.suite = True, [BROKEN if first else OK]
        check.regressions = ["pytest"] if first else []
        check.problems = ["these passed before the change and fail now: pytest"] if first else []
        check.passed = not first

    async def nothing(*args: Any, **kwargs: Any) -> Any:
        return None

    async def no_diff(*args: Any, **kwargs: Any) -> str:
        return ""

    monkeypatch.setattr(worker, "prepare_repo", prepare_repo)
    monkeypatch.setattr(worker, "ensure_env_image", nothing)
    monkeypatch.setattr(worker, "run_commands", run_commands)
    monkeypatch.setattr(worker, "save_recipe", nothing)
    monkeypatch.setattr(worker.archmap, "load_map", nothing)
    monkeypatch.setattr(worker, "change_context", nothing)
    monkeypatch.setattr(worker, "change_markdown", lambda *a, **k: ("", 0))
    monkeypatch.setattr(worker, "diff", no_diff)
    monkeypatch.setattr(worker, "draft_pr", nothing)
    monkeypatch.setattr(worker, "check_suites", check_suites)
    monkeypatch.setattr(worker.reviewer, "review", review)
    monkeypatch.setattr(CoderRun, "work", work)

    done = await worker.run_task(approved)

    assert order == ["baseline", "coder", "review", "suites", "coder", "review", "suites"]
    assert done.status == "ready"
    checks = [(c["check"], c["ok"], c["round"]) for c in done.result["checks"]]
    assert checks == [
        ("scope", True, 1), ("test", True, 1), ("review", True, 1), ("suites", False, 1),
        ("scope", True, 2), ("test", True, 2), ("review", True, 2), ("suites", True, 2),
    ]  # fmt: skip
