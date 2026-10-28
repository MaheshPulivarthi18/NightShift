"""Runs whose server went away are marked interrupted, never left showing as running.
Uses the local Postgres (docker compose up); skipped when it is not running."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from nightshift import events
from nightshift.db import Repo, Run, RunEvent, session_scope


async def test_a_run_with_no_activity_is_marked_interrupted() -> None:
    name = f"test-int/{uuid.uuid4().hex[:8]}"
    old, busy, fresh = (f"setup-test-{uuid.uuid4().hex[:8]}" for _ in range(3))
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name)
            s.add(repo)
            await s.flush()
            s.add_all([
                Run(run_id=old, repo_id=repo.id, status="running", started_at=long_ago),  # the server died
                Run(run_id=busy, repo_id=repo.id, status="running", started_at=long_ago),  # long, but still logging
                Run(run_id=fresh, repo_id=repo.id, status="running"),
            ])  # fmt: skip
            await s.flush()
            s.add(RunEvent(run_id=busy, kind="node_start", node="setup", message="attempt 3", data={}))
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    try:
        marked = await events.mark_interrupted_runs(timedelta(minutes=15))
        assert old in marked and busy not in marked and fresh not in marked
        async with session_scope() as s:
            run = await s.get(Run, old)
            assert run is not None and run.status == "interrupted" and run.error == events.INTERRUPTED
            assert (await s.get(Run, busy)).status == "running"  # type: ignore[union-attr]
    finally:
        async with session_scope() as s:
            await s.execute(delete(Repo).where(Repo.full_name == name))
