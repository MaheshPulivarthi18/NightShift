"""Run tracking for the Jobs page: run status plus a timeline of node, model-call and sandbox events.

Events go to Postgres, so the web app sees runs started from the CLI as well as its own.
Recording is best-effort: a failure to write an event is logged and never fails the run.
"""

from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, func, or_, select, update

from nightshift.db import Run, RunEvent, session_scope

log = structlog.get_logger()

CAUGHT = "caught"  # a check found a problem and NightShift is already handling it (re-plan, retry, fallback)
FIXED = "fixed"  # the problem a CAUGHT event named was handled

# What happened in the sandbox, so the log shows the work and not only that a model took a step:
CMD_START = "cmd_start"  # a command started (message: the command; data.cmd ties its events together)
CMD_OUTPUT = "cmd_output"  # new output while it runs (message: the text)
CMD_END = "cmd_end"  # it finished (data: exit, seconds, timed_out, tail)
ACTION = "action"  # a file read, searched, written or edited (data.tool, data.path)

# The run this code is working for: set by start_run, so the sandbox can log its commands to it without
# every caller passing a run id. Awaited code shares it; a task started from here copies it.
_current: ContextVar[str | None] = ContextVar("nightshift_run", default=None)

# The sandbox holds no keys, but a repo's own output can echo one (a test fixture, a misconfigured CI file).
# Anything token-shaped is masked before it is stored.
_SECRET = re.compile(
    r"sk-(?:or-|ant-|proj-)?[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
    r"|(?<=[Bb]earer )[A-Za-z0-9._\-]{16,}"
)


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


def current_run() -> str | None:
    return _current.get()


async def action(tool: str, message: str, **data: Any) -> None:
    """A file read, searched, written or edited in the sandbox, on the current run's log."""
    await record(_current.get(), ACTION, message, tool=tool, **data)


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
    _current.set(run_id)


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
    if _current.get() == run_id:
        _current.set(None)
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
