"""Setting a repo up again works out a blocked environment again; a ready one is kept.
Uses the test database for the run record; the sandbox and agents are faked."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import delete

from nightshift.db import Repo, session_scope
from nightshift.worker import onboard
from nightshift.worker.setup_agent import Environment


@pytest.mark.parametrize(("before", "forced"), [("blocked", True), ("failed", True), ("ready", False), (None, False)])
async def test_setting_up_again_retries_what_was_blocked(
    before: str | None, forced: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = f"test-onboard/{uuid.uuid4().hex[:8]}"
    seen: list[bool] = []

    class Box:
        async def exec(self, command: str, **kw: Any) -> Any:
            return type("R", (), {"stdout": "abc123\n"})()

        async def __aenter__(self) -> Box:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

    async def prepare(*a: Any, **k: Any) -> tuple[Box, bool]:
        return Box(), True

    async def previous(repo_id: int) -> Environment | None:
        return Environment(status=before, commit="abc123") if before else None  # type: ignore[arg-type]

    async def ensure(llm: Any, repo: Any, box: Any, commit: str, *, force: bool = False) -> Environment:
        seen.append(force)
        return Environment(status="ready", commit=commit)

    async def nothing(*a: Any, **k: Any) -> Any:
        return None, set()

    monkeypatch.setattr(onboard, "prepare_repo", prepare)
    monkeypatch.setattr(onboard, "load_environment", previous)
    monkeypatch.setattr(onboard, "ensure_environment", ensure)
    monkeypatch.setattr(onboard.archmap, "architecture_map", nothing)
    monkeypatch.setattr(onboard, "repo_brief", nothing)
    try:
        await onboard.onboard(name)
        assert seen == [forced]
    finally:
        async with session_scope() as s:
            await s.execute(delete(Repo).where(Repo.full_name == name))
