"""A partial fetch (GitHub kept timing out on a list) keeps the repo's "fetched up to" mark, so the next run asks
for what was missed. Uses the local Postgres (docker compose up); skipped when it is not running."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from nightshift.db import Repo, session_scope
from nightshift.memory import upsert_items

ITEM = {
    "number": 1, "kind": "issue", "title": "t", "body": "", "author": "a", "state": "open", "state_reason": None,
    "url": "u", "labels": [], "created_at": datetime(2026, 10, 1, tzinfo=UTC),
    "updated_at": datetime(2026, 10, 2, tzinfo=UTC), "closed_at": None, "data": {},
}  # fmt: skip


async def test_only_a_complete_fetch_moves_the_mark() -> None:
    name = f"test-mark/{uuid.uuid4().hex[:8]}"
    before = datetime(2026, 10, 1, tzinfo=UTC)
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name, last_fetched_at=before)
            s.add(repo)
            await s.flush()
            await upsert_items(s, repo, [ITEM], complete=False)
            assert repo.last_fetched_at == before
            await upsert_items(s, repo, [ITEM])
            assert repo.last_fetched_at is not None and repo.last_fetched_at > before
    except OSError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    finally:
        async with session_scope() as s:
            await s.execute(delete(Repo).where(Repo.full_name == name))
