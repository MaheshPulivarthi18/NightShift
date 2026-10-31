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


@pytest.fixture(autouse=True)
def reports_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Task folders and reports a test writes go to its own temporary folder, never the real reports/."""
    from nightshift.config import get_settings

    monkeypatch.setattr(get_settings(), "reports_dir", str(tmp_path / "reports"))


@pytest.fixture(autouse=True)
def no_onboarding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a repo through the API starts its setup in a real sandbox; tests that add repos must not."""
    from nightshift.web import api

    monkeypatch.setattr(api, "_start_onboard", lambda repo, run_id: None)


@pytest.fixture(autouse=True)
async def no_stray_run_events() -> Any:
    """Run events a test wrote for runs it has since deleted stay behind (they do not cascade). Remove those,
    and only those: events of real runs, even ones running while the tests run, keep their run row."""
    from sqlalchemy import delete, func, select

    from nightshift.db import Run, RunEvent, session_scope

    try:
        async with session_scope() as s:
            start = (await s.execute(select(func.coalesce(func.max(RunEvent.id), 0)))).scalar_one()
    except Exception:  # no database: nothing to clean
        yield
        return
    yield
    async with session_scope() as s:
        await s.execute(delete(RunEvent).where(RunEvent.id > start, RunEvent.run_id.not_in(select(Run.run_id))))


@pytest.fixture(autouse=True)
def budgets_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spend caps are checked against the real ledger, so a busy day of real work (benchmarks) would refuse the
    tests' fake model calls. Tests get room; a test about budgets sets its own caps, which win."""
    from nightshift.config import get_settings

    monkeypatch.setattr(get_settings(), "daily_budget_usd", 1_000_000.0)
    monkeypatch.setattr(get_settings(), "monthly_budget_usd", 1_000_000.0)
