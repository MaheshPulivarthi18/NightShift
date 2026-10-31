"""Tests use their own database, `nightshift_test` beside the dev one (or NIGHTSHIFT_TEST_DATABASE_URL),
created and migrated when the run starts: the dev database holds real tasks, runs and spend, and tests once
deleted real run events and hit the real daily cap."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest


def _test_database_url() -> str:
    from nightshift.config import Settings

    url = (
        os.environ.get("NIGHTSHIFT_TEST_DATABASE_URL") or Settings().database_url.rsplit("/", 1)[0] + "/nightshift_test"
    )
    if url == Settings().database_url:
        raise RuntimeError("The test database must not be the dev database.")
    return url


# before anything reads the settings (they are cached): every test, and the app under test, use this database
os.environ["NIGHTSHIFT_DATABASE_URL"] = _test_database_url()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Create the test database if needed and bring it to the latest migration. No Postgres: the tests that
    need one skip themselves."""
    import asyncio

    import psycopg
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["NIGHTSHIFT_DATABASE_URL"])
    plain = url.set(drivername="postgresql")
    try:
        admin = psycopg.connect(plain.set(database="postgres").render_as_string(hide_password=False),
                                autocommit=True, connect_timeout=3)  # fmt: skip
    except psycopg.OperationalError:
        return
    with admin:
        if not admin.execute("select 1 from pg_database where datname = %s", (url.database,)).fetchone():
            admin.execute(f'create database "{url.database}"')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    command.upgrade(Config(os.path.join(root, "alembic.ini")), "head")

    from nightshift.health.graph import setup_checkpointer

    asyncio.run(setup_checkpointer())  # LangGraph's own tables, as `nightshift db-setup` makes them


from fake_openrouter import models  # noqa: F401, E402  (the fixed model catalog fixture)

from nightshift import outcomes  # noqa: E402  (after the database is chosen)
from nightshift.db import Item  # noqa: E402

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
