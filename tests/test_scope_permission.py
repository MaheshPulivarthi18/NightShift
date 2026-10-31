"""When the scope check refuses a lock or generated file the change truly needs (a dependency upgrade re-locks),
the Coder can ask, the run stops for the developer, and their answer continues it. Size limits are settings,
set from the PRs maintainers merged. Uses the local Postgres for the answer (docker compose up)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete

from nightshift.config import get_settings
from nightshift.db import Repo, Task, session_scope
from nightshift.worker import task as worker
from nightshift.worker.coder import CoderRun
from nightshift.worker.verify import FixCheck, Scope, ScopeCheck, check_scope


def test_files_the_developer_allowed_pass_the_scope_check() -> None:
    lines = {"engine/requirements.txt": 2, "engine/requirements.lock": 630, "scripts/check-locks.sh": 3}
    refused = check_scope(lines, Scope(planned=["engine/requirements.txt"], size="small"))
    assert refused.generated == ["engine/requirements.lock"] and "keep_generated" in refused.problems[0]
    allowed = check_scope(lines, Scope(planned=["engine/requirements.txt"], size="small",
                                       allowed=["engine/requirements.lock"]))  # fmt: skip
    assert not allowed.problems and allowed.lines == 5


def test_size_limits_are_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_settings().scope_limits == {"small": 250, "medium": 600, "large": 2500}
    monkeypatch.setattr(get_settings(), "scope_limits", {"small": 10, "medium": 600, "large": 2500})
    assert check_scope({"a.py": 11}, Scope(size="small")).problems


def _check(generated: list[str], lines: int = 5, limit: int = 250) -> FixCheck:
    scope = ScopeCheck(lines=lines, limit=limit, generated=generated, problems=["refused"])
    return FixCheck(passed=False, test_files=[], source_files=[], scope=scope, problems=["refused"])


def test_the_coder_can_ask_only_for_generated_files_it_names() -> None:
    coder = CoderRun(None, None, "abc", [], [], "")  # type: ignore[arg-type]
    keep = {"keep_generated": {"files": ["uv.lock"], "reason": "litellm 1.104 needs a re-lock"}}
    assert coder._asks_to_keep(_check(["uv.lock"]), keep) is not None
    assert coder.permission == {"files": ["uv.lock"], "reason": "litellm 1.104 needs a re-lock"}
    coder.permission = None
    assert coder._asks_to_keep(_check(["uv.lock", "go.sum"]), keep) is None  # go.sum not named
    assert coder._asks_to_keep(_check(["uv.lock"], lines=900), keep) is None  # over the size limit: never kept
    assert coder._asks_to_keep(_check(["uv.lock"]), {}) is None and coder.permission is None


@pytest.fixture
async def asking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[int]:
    monkeypatch.setattr(get_settings(), "reports_dir", str(tmp_path))
    name = f"test-permission/{uuid.uuid4().hex[:8]}"
    plan = {"markdown": "# Plan", "setup_commands": [], "test_commands": ["pytest"],
            "files_to_change": [{"path": "engine/requirements.txt", "why": "pin"}]}  # fmt: skip
    result: dict[str, Any] = {
        "permission": {"files": ["engine/requirements.lock"], "reason": "re-locked"},
        "checks": [{"check": "scope", "ok": False, "round": 1, "seconds": 0, "detail": "x"}],
        "check": {"passed": False, "test_files": [], "source_files": [], "problems": ["x"]},
    }
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name)
            s.add(repo)
            await s.flush()
            t = Task(repo_id=repo.id, issue_number=50, status="needs_operator", base_commit="abc", plan=plan,
                     result=result, issue={}, diff="+litellm==1.104.2")  # fmt: skip
            s.add(t)
            await s.flush()
            task_id = t.id
    except OSError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    yield task_id
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_allowing_adds_the_files_to_the_plan_and_continues(asking: int) -> None:
    task = await worker.answer_permission(asking, allow=True)
    assert task.status == "approved" and task.plan["allowed_generated"] == ["engine/requirements.lock"]
    assert "permission" not in task.result
    assert task.result["resume"]["todo"].startswith("The developer allowed the change to regenerate")


async def test_refusing_tells_the_coder_to_restore_them(asking: int) -> None:
    task = await worker.answer_permission(asking, allow=False)
    assert "allowed_generated" not in task.plan
    assert "restore them with `git checkout abc -- <file>`" in task.result["resume"]["todo"]
    with pytest.raises(worker.TaskError, match="not waiting"):
        await worker.answer_permission(asking, allow=True)  # answered already
