"""JSON API for contribution tasks: plan, Gate 1, run, PR draft, claim and Gate 2.

Gate 2 pushes code and opens a PR under the operator's name, so every write is guarded
(web/common.py) and publishing needs an explicit confirm.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from nightshift.db import Repo, Task, session_scope
from nightshift.web.common import Guard, render_markdown
from nightshift.worker import task as worker
from nightshift.worker.publish import PublishError

log = structlog.get_logger()
router = APIRouter(prefix="/api")
_jobs: dict[str, asyncio.Task[Any]] = {}

STAGES = {
    "planning": "plan", "planned": "gate1", "approved": "work", "working": "work", "ready": "gate2",
    "published": "done", "rejected": "closed", "needs_operator": "stopped", "failed": "stopped",
}  # fmt: skip


def _background(key: str, coro: Any) -> None:
    if key in _jobs and not _jobs[key].done():
        raise HTTPException(409, "Already running")

    async def job() -> None:
        try:
            await coro
        except Exception as e:  # recorded on the task; keep the server alive
            log.warning("task_job_failed", job=key, error=str(e))

    _jobs[key] = asyncio.create_task(job())


def _md(text: str, repo: str | None = None) -> str:
    return render_markdown(text or "", repo)


def _summary(t: Task, r: Repo) -> dict[str, Any]:
    return {
        "id": t.id,
        "repo": r.full_name,
        "issue": t.issue_number,
        "title": t.issue.get("title", ""),
        "status": t.status,
        "stage": STAGES.get(t.status, "stopped"),
        "rehearsal": t.rehearsal,
        "cost_usd": round(t.cost_usd, 4),
        "error": t.error,
        "busy": f"task-{t.id}" in _jobs and not _jobs[f"task-{t.id}"].done(),
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@router.get("/tasks")
async def list_tasks() -> list[dict[str, Any]]:
    async with session_scope() as s:
        rows = (await s.execute(select(Task, Repo).join(Repo).order_by(Task.id.desc()).limit(200))).all()
    return [_summary(t, r) for t, r in rows]


@router.get("/tasks/{task_id}")
async def get_task(task_id: int) -> dict[str, Any]:
    try:
        t, r = await worker.load_task(task_id)
    except worker.TaskError as e:
        raise HTTPException(404, str(e)) from e
    res = t.result or {}
    pr = res.get("pr")
    return {
        **_summary(t, r),
        "issue_url": t.issue.get("url"),
        "run_id": t.run_id,
        "base_commit": t.base_commit,
        "note": t.note,
        "plan_html": _md(t.plan.get("markdown", ""), r.full_name),
        "setup_commands": t.plan.get("setup_commands", []),
        "test_commands": t.plan.get("test_commands", []),
        "claim_comment": t.plan.get("claim_comment", ""),
        "claim_url": res.get("claim_url"),
        "baseline": res.get("baseline", []),
        "check": res.get("check"),
        "reviews": [
            {"round": rv["round"], "verdict": rv["verdict"], "html": _md(rv["markdown"])}
            for rv in res.get("reviews", [])
        ],
        "diff": t.diff or res.get("partial_diff") or "",
        "pr": {"title": pr["title"], "html": _md(pr["body"], r.full_name), "commit": pr["commit_message"]}
        if pr
        else None,
        "pr_url": res.get("pr_url"),
        "coder_steps": res.get("coder_steps"),
    }


class NewTask(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    issue: int = Field(gt=0)
    base: str | None = None
    rehearsal: bool = False


@router.post("/tasks", status_code=202, dependencies=[Guard])
async def new_task(body: NewTask) -> dict[str, str]:
    """Checks the issue at once (exists, still free), then plans in the background."""
    try:
        current, _, _, moved = await worker.precheck(body.repo, body.issue, body.rehearsal)
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e
    body.repo = current
    _background(f"plan-{body.repo}-{body.issue}", worker.plan_task(body.repo, body.issue, base=body.base or None,
                                                                    rehearsal=body.rehearsal))  # fmt: skip
    return {"status": "planning", "repo": current, "note": moved or ""}


class Decision(BaseModel):
    approve: bool
    note: str | None = None
    setup_commands: list[str] = []
    test_commands: list[str] = []


@router.post("/tasks/{task_id}/decision", dependencies=[Guard])
async def decide(task_id: int, body: Decision) -> dict[str, Any]:
    try:
        await worker.decide(task_id, body.approve, body.note, setup_commands=body.setup_commands,
                            test_commands=body.test_commands)  # fmt: skip
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e
    return await get_task(task_id)


@router.post("/tasks/{task_id}/run", status_code=202, dependencies=[Guard])
async def run(task_id: int) -> dict[str, str]:
    t, _ = await worker.load_task(task_id)
    if t.status != "approved":
        raise HTTPException(409, f"Task is {t.status}; only an approved plan can run.")
    _background(f"task-{task_id}", worker.run_task(task_id))
    return {"status": "working"}


class Revision(BaseModel):
    note: str
    latest: bool = False
    setup_commands: list[str] = []
    test_commands: list[str] = []
    run: bool = True


@router.post("/tasks/{task_id}/revise", dependencies=[Guard])
async def revise(task_id: int, body: Revision) -> dict[str, Any]:
    """Send a ready or stopped task back with a correction (optionally on the latest commit), and run it."""
    try:
        await worker.revise(task_id, body.note, body.setup_commands or None, body.test_commands or None, body.latest)
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e
    if body.run:
        _background(f"task-{task_id}", worker.run_task(task_id))
    return await get_task(task_id)


class Confirm(BaseModel):
    confirm: bool = False


@router.post("/tasks/{task_id}/claim", dependencies=[Guard])
async def claim(task_id: int, body: Confirm) -> dict[str, str]:
    try:
        return {"result": await worker.claim(task_id, confirm=body.confirm)}
    except (worker.TaskError, PublishError) as e:
        raise HTTPException(409, str(e)) from e


@router.post("/tasks/{task_id}/publish", dependencies=[Guard])
async def publish(task_id: int, body: Confirm) -> dict[str, Any]:
    """Gate 2. Without confirm: freshness and a dry run on the current upstream branch."""
    try:
        info = await worker.gate2(task_id, confirm=body.confirm)
    except (worker.TaskError, PublishError) as e:
        raise HTTPException(409, str(e)) from e
    info.pop("body", None)
    return info
