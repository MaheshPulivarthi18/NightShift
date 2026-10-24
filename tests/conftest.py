from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fake_openrouter import models  # noqa: F401  (the fixed model catalog fixture)

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
