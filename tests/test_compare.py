"""The watchlist comparison: each watched repo's latest contributor report, as numbers to rank by.
Uses the local Postgres (docker compose up); skipped when it is not running."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete

from nightshift.db import Item, Repo, Report, session_scope
from nightshift.web import compare_api
from nightshift.web.app import app


def _opp(n: int, size: str, **kw: Any) -> dict[str, Any]:
    return {"number": n, "kind": "bug", "size": size, "summary": f"fix {n}", "first_step": "read it",
            "actionable": True, "maintainer_declined": False, "needs_maintainer_ok": False, "outsider_can_do": True,
            "claimed": False, **kw}  # fmt: skip


@pytest.fixture
async def watched() -> AsyncIterator[tuple[str, str]]:
    fresh, stale = (f"test-compare/{uuid.uuid4().hex[:8]}" for _ in range(2))
    try:
        async with session_scope() as s:
            a, b = Repo(full_name=fresh), Repo(full_name=stale)
            s.add_all([a, b])
            await s.flush()
            findings = {
                "view": "contributor",
                "pulse": {"verdict": "welcoming", "merge_rate": 0.8, "outside_prs_merged": 8,
                          "outside_prs_closed_unmerged": 2, "outside_prs_waiting": 1, "median_pr_response_days": 0.9},
                "brief": {"pr_rules": ["Disclose AI-generated commit content with an `Assisted-by:` trailer.",
                                       "Do not edit generated files such as *.pb.go.",
                                       "Put provider tests under `tests/llm/`."]},
                "opportunities": [_opp(1, "small"), _opp(2, "large"), _opp(3, "small", claimed=True),
                                  _opp(4, "medium", needs_maintainer_ok=True)],
            }  # fmt: skip
            s.add(Report(repo_id=a.id, run_id=uuid.uuid4().hex, findings=findings, markdown=""))
            s.add(Item(repo_id=a.id, number=1, kind="issue", title="Labels are not removed", body="", state="open",
                       url="u", labels=[], data={}, content_hash="h", created_at=datetime.now(UTC),
                       updated_at=datetime.now(UTC)))  # fmt: skip
            old = Report(repo_id=b.id, run_id=uuid.uuid4().hex, findings={"view": "contributor"}, markdown="")
            old.created_at = datetime.now(UTC) - timedelta(days=30)
            s.add(old)
    except OSError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    yield fresh, stale
    compare_api._refresh.clear()
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name.in_([fresh, stale])))


async def test_a_repo_row_has_its_reports_numbers(watched: tuple[str, str]) -> None:
    fresh, _ = watched
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        rows = {r["repo"]: r for r in (await c.get("/api/compare")).json()}
    row = rows[fresh]
    assert row["pulse"]["verdict"] == "welcoming"
    # Only issues an outsider can take without asking: not the claimed one, not the one needing a maintainer's ok
    assert [(s["number"], s["size"], s["title"]) for s in row["starters"]] == [
        (1, "small", "Labels are not removed"), (2, "large", "")
    ]  # fmt: skip
    assert row["ask_first"] == 1
    assert row["ai_rules"] == ["Disclose AI-generated commit content with an `Assisted-by:` trailer."]


async def test_refresh_runs_only_missing_and_stale_reports(
    watched: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh, stale = watched
    ran: list[list[str]] = []

    async def run_all(repos: list[str]) -> None:
        ran.append(repos)

    monkeypatch.setattr(compare_api, "_run_all", run_all)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/compare/refresh", json={"stale_days": 7}, headers={"Origin": "http://test"})
    queued = resp.json()["queued"]
    assert stale in queued and fresh not in queued
    assert compare_api._refresh[stale] == "queued"
