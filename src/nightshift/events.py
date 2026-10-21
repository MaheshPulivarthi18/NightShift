"""Run tracking for the Jobs page: run status plus a timeline of node and model-call events.

Events go to Postgres, so the web app sees runs started from the CLI as well as its own.
Recording is best-effort: a failure to write an event is logged and never fails the run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

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
