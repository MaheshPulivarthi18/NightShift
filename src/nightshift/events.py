"""Run tracking for the Jobs page: run status plus a timeline of node and model-call events.

Events go to Postgres, so the web app sees runs started from the CLI as well as its own.
Recording is best-effort: a failure to write an event is logged and never fails the run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, func, or_, select, update

from nightshift.db import Run, RunEvent, session_scope

log = structlog.get_logger()

CAUGHT = "caught"  # a check found a problem and NightShift is already handling it (re-plan, retry, fallback)
FIXED = "fixed"  # the problem a CAUGHT event named was handled

BEAT = timedelta(seconds=30)  # how often a running run says it is alive
STALE = timedelta(minutes=3)  # no beat for this long: the process running it is gone
_beats: dict[str, asyncio.Task[None]] = {}  # this process's running runs


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
            s.add(Run(run_id=run_id, repo_id=repo_id, status="running", heartbeat_at=datetime.now(UTC)))
        else:  # a resumed run
            run.status, run.error, run.finished_at, run.heartbeat_at = "running", None, None, datetime.now(UTC)
    if old := _beats.pop(run_id, None):
        old.cancel()
    _beats[run_id] = asyncio.create_task(_beat(run_id))


async def _beat(run_id: str) -> None:
    """Mark the run alive until it finishes. A long step with nothing to log (a 30-minute install) is still
    alive; a closed or killed process stops beating, and mark_interrupted_runs notices within STALE."""
    while True:
        await asyncio.sleep(BEAT.total_seconds())
        try:
            async with session_scope() as s:
                await s.execute(
                    update(Run).where(Run.run_id == run_id, Run.status == "running").values(heartbeat_at=func.now())
                )
        except Exception as e:  # a missed beat is retried; tracking must never break a run
            log.warning("run_heartbeat_not_recorded", run_id=run_id, error=str(e))


async def finish_run(run_id: str, status: str, error: str | None = None, report_path: str | None = None) -> None:
    if beat := _beats.pop(run_id, None):
        beat.cancel()
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


async def mark_interrupted_runs(idle: timedelta = timedelta(minutes=15), stale: timedelta = STALE) -> list[str]:
    """Runs still marked running whose process is gone (closed, killed or crashed) without recording it: no
    heartbeat for `stale`. They become "interrupted", so nothing shows as running forever. Runs from before
    heartbeats have only their events to go by: no activity for `idle`. Returns their ids."""
    now = datetime.now(UTC)
    async with session_scope() as s:
        last = select(RunEvent.run_id, func.max(RunEvent.created_at).label("at")).group_by(RunEvent.run_id).subquery()
        rows = (
            (
                await s.execute(
                    select(Run)
                    .outerjoin(last, last.c.run_id == Run.run_id)
                    .where(
                        Run.status == "running",
                        or_(
                            Run.heartbeat_at < now - stale,
                            and_(
                                Run.heartbeat_at.is_(None),
                                func.coalesce(last.c.at, Run.started_at) < now - idle,
                            ),
                        ),
                    )
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
