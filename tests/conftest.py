from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fake_openrouter import models  # noqa: F401  (the fixed model catalog fixture)

from nightshift import outcomes
from nightshift.db import Item

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def make_item(number: int, kind: str = "pr", state: str = "OPEN", days_old: int = 10, **data: Any) -> Item:
    created = NOW - timedelta(days=days_old)
    item = Item(
        repo_id=1,
        number=number,
        kind=kind,
        title=f"{kind} {number}",
        body="",
        author="alice",
        state=state,
        state_reason=data.pop("state_reason", None),
        url=f"https://github.com/o/r/pull/{number}",
        labels=[],
        created_at=created,
        updated_at=data.pop("updated_at", created),
        closed_at=None,
        data={"comments": [], **data},
        content_hash=f"h{number}",
    )
    return item


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture(autouse=True)
def no_learning(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Tests never write model outcomes to the shared dev database, where they would steer the real
    router. A test that checks outcomes themselves is marked real_outcomes and cleans up after itself."""
    if "real_outcomes" in request.keywords:
        return

    async def record(*_: Any, **__: Any) -> None:
        return None

    async def stats(_: str) -> dict[str, outcomes.ModelStats]:
        return {}

    monkeypatch.setattr(outcomes, "record", record)
    monkeypatch.setattr(outcomes, "stats", stats)
