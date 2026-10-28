"""Run tracking for the Jobs page: run status plus a timeline of node and model-call events.

Events go to Postgres, so the web app sees runs started from the CLI as well as its own.
Recording is best-effort: a failure to write an event is logged and never fails the run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select

from nightshift.db import Run, RunEvent, session_scope

log = structlog.get_logger()


async def record(run_id: str | None, kind: str, message: str = "", node: str | None = None, **data: Any) -> None:
    if not run_id:
        return
    try:
        async with session_scope() as s:
            s.add(RunEvent(run_id=run_id, kind=kind, node=node, message=message, data=data))
            if kind == "node_start":
                run = await s.get(Run, run_id)
                if run is not None:
                    run.current_node = node
    except Exception as e:  # tracking must never break a run
        log.warning("run_event_not_recorded", run_id=run_id, kind=kind, error=str(e))


async def start_run(run_id: str, repo_id: int) -> None:
    async with session_scope() as s:
        run = await s.get(Run, run_id)
        if run is None:
            s.add(Run(run_id=run_id, repo_id=repo_id, status="running"))
        else:  # a resumed run
            run.status, run.error, run.finished_at = "running", None, None


async def finish_run(run_id: str, status: str, error: str | None = None, report_path: str | None = None) -> None:
    try:
        async with session_scope() as s:
            run = await s.get(Run, run_id)
            if run is None:
                return
            run.status, run.error, run.finished_at = status, error, datetime.now(UTC)
            run.current_node = None
            if report_path:
                run.report_path = report_path
    except Exception as e:
        log.warning("run_status_not_recorded", run_id=run_id, error=str(e))


INTERRUPTED = "Interrupted: NightShift was closed while this was running."


async def mark_interrupted_runs(idle: timedelta = timedelta(minutes=15)) -> list[str]:
    """Runs still marked running with no activity for `idle`: the process that ran them is gone (closed,
    killed or crashed) without recording it. They become "interrupted", so nothing shows as running forever.
    Returns their ids."""
    cutoff = datetime.now(UTC) - idle
    async with session_scope() as s:
        last = select(RunEvent.run_id, func.max(RunEvent.created_at).label("at")).group_by(RunEvent.run_id).subquery()
        rows = (
            (
                await s.execute(
                    select(Run)
                    .outerjoin(last, last.c.run_id == Run.run_id)
                    .where(Run.status == "running", func.coalesce(last.c.at, Run.started_at) < cutoff)
                )
            )
            .scalars()
            .all()
        )
        for run in rows:
            run.status, run.error, run.finished_at, run.current_node = (
                "interrupted",
                INTERRUPTED,
                datetime.now(UTC),
                None,
            )
    return [r.run_id for r in rows]
