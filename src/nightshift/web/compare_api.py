"""Compare the watchlist: where to contribute first.

One row per watched repo, from its latest contributor report: how outside PRs fare (merged, closed, waiting
unanswered), how fast maintainers respond, the free starter issues by size, and what the repo's PR rules say
about AI. Refreshing runs the missing and stale reports a few at a time. Whether a starter issue is still
free is checked live, per repo, by the report's own live check (`/repos/{o}/{n}/report/live`).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from nightshift.db import Item, Repo, Report, session_scope
from nightshift.health.graph import run_health_report
from nightshift.health.report import starters
from nightshift.web.api import _iso
from nightshift.web.common import Guard

log = structlog.get_logger()
router = APIRouter(prefix="/api")

STALE_DAYS = 7
AT_ONCE = 2  # GitHub's search API refuses bursts; reports also call models
# A PR rule about AI use. "AI-generated" counts; "generated files" (protobuf, mocks) or a path like
# `tests/llm/` does not.
AI_RULE = re.compile(r"\bAI\b|(?<![/\w.])LLMs?\b|\bcopilot\b|\bchatgpt\b|assisted-by|machine-generated", re.I)

_refresh: dict[str, str] = {}  # repo -> queued | running | done | failed, for the refresh in progress


def _row(repo: Repo, report: Report | None, titles: dict[int, str]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "repo": repo.full_name,
        "refresh": _refresh.get(repo.full_name),
        "report_at": None,
        "run_id": None,
        "pulse": None,
        "starters": [],
        "ask_first": 0,
        "ai_rules": [],
    }
    if report is None or (report.findings or {}).get("view") != "contributor":
        return row
    f = report.findings
    start = starters(f.get("opportunities", []))
    row.update(
        report_at=_iso(report.created_at),
        run_id=report.run_id,
        pulse=f.get("pulse"),
        starters=[
            {
                "number": o["number"],
                "title": titles.get(o["number"], ""),
                "size": o["size"],
                "kind": o["kind"],
                "summary": o.get("summary", ""),
                "first_step": o.get("first_step", ""),
            }
            for o in start
        ],  # fmt: skip
        ask_first=sum(1 for o in f.get("opportunities", []) if o.get("needs_maintainer_ok") and o.get("actionable")),
        ai_rules=[r for r in (f.get("brief") or {}).get("pr_rules", []) if AI_RULE.search(r)],
    )
    return row


@router.get("/compare")
async def compare() -> list[dict[str, Any]]:
    """Every watched repo with its latest contributor report's numbers, or none yet."""
    async with session_scope() as s:
        repos = list((await s.execute(select(Repo).where(Repo.watched.is_(True)).order_by(Repo.full_name))).scalars())
        latest: dict[int, Report] = {}
        for rep in (await s.execute(select(Report).order_by(Report.created_at))).scalars():
            latest[rep.repo_id] = rep  # the newest wins
        numbers = {
            o["number"]
            for rep in latest.values()
            for o in (rep.findings or {}).get("opportunities", [])
            if isinstance(o, dict) and "number" in o
        }
        titles: dict[tuple[int, int], str] = {}
        if numbers:
            rows = await s.execute(
                select(Item.repo_id, Item.number, Item.title).where(
                    Item.repo_id.in_([r.id for r in repos]), Item.number.in_(numbers)
                )
            )
            titles = {(rid, n): t for rid, n, t in rows.all()}
    return [_row(r, latest.get(r.id), {n: t for (rid, n), t in titles.items() if rid == r.id}) for r in repos]


class RefreshIn(BaseModel):
    stale_days: int = STALE_DAYS


@router.post("/compare/refresh", dependencies=[Guard])
async def refresh(body: RefreshIn) -> dict[str, Any]:
    """Run the report of every watched repo with none, or one older than `stale_days`, AT_ONCE at a time."""
    if any(v in ("queued", "running") for v in _refresh.values()):
        return {"queued": [r for r, v in _refresh.items() if v in ("queued", "running")], "already": True}
    cutoff = datetime.now(UTC) - timedelta(days=body.stale_days)
    rows = await compare()
    todo = [r["repo"] for r in rows if not r["report_at"] or datetime.fromisoformat(r["report_at"]) < cutoff]
    _refresh.clear()
    _refresh.update({repo: "queued" for repo in todo})
    if todo:
        asyncio.create_task(_run_all(todo))
    return {"queued": todo, "already": False}


async def _run_all(repos: list[str]) -> None:
    gate = asyncio.Semaphore(AT_ONCE)

    async def one(repo: str) -> None:
        async with gate:
            _refresh[repo] = "running"
            try:
                await run_health_report(repo, run_id=uuid.uuid4().hex)
                _refresh[repo] = "done"
            except Exception as e:  # recorded on the run; the rest go on
                log.warning("compare_refresh_failed", repo=repo, error=str(e))
                _refresh[repo] = "failed"

    await asyncio.gather(*(one(r) for r in repos))
