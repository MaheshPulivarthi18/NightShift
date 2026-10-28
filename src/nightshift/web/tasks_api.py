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

from nightshift.config import model_id
from nightshift.db import LLMCall, Repo, Task, session_scope
from nightshift.router import CHOOSABLE, agent_routes, choices
from nightshift.web.common import Guard, model_view, render_markdown
from nightshift.worker import task as worker
from nightshift.worker.prcheck import PRCheckError
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
        "cost_usd": round(t.cost_usd, 4),
        "error": t.error,
        "busy": f"task-{t.id}" in _jobs and not _jobs[f"task-{t.id}"].done(),
        "archived": t.archived_at is not None,
        # unanswered review feedback found by the background sync on the task's open PR
        "feedback": len(((t.result or {}).get("sync") or {}).get("open") or [])
        if ((t.result or {}).get("sync") or {}).get("pr_state") == "OPEN"
        else 0,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@router.get("/tasks")
async def list_tasks(archived: bool = False) -> list[dict[str, Any]]:
    """The board's tasks; `archived=true` lists the archived ones instead."""
    await worker.mark_stale_runs()
    cond = Task.archived_at.is_not(None) if archived else Task.archived_at.is_(None)
    async with session_scope() as s:
        rows = (await s.execute(select(Task, Repo).join(Repo).where(cond).order_by(Task.id.desc()).limit(200))).all()
    return [_summary(t, r) for t, r in rows]


class Archive(BaseModel):
    archived: bool = True


@router.post("/tasks/{task_id}/archive", dependencies=[Guard])
async def archive(task_id: int, body: Archive) -> dict[str, Any]:
    try:
        await worker.archive(task_id, body.archived)
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e
    return await get_task(task_id)


async def _model_choices() -> dict[str, Any]:
    """For Gate 1: per choosable agent, the model the router would try first and the ones to pick from."""
    out: dict[str, Any] = {}
    for agent in CHOOSABLE:
        first = (await agent_routes(agent))[0]
        out[agent] = {
            "default": first.info.id,
            "why": first.why,
            "options": [model_view(m) for m in await choices(agent)],
        }
    return out


async def _models_used(run_id: str) -> list[dict[str, Any]]:
    """Which models each agent used in this task, in order, with what they cost (from the ledger)."""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(LLMCall.agent, LLMCall.model, LLMCall.cost_usd, LLMCall.error)
                .where(LLMCall.run_id == run_id)
                .order_by(LLMCall.id)
            )
        ).all()
    agents: dict[str, dict[str, Any]] = {}
    for agent, name, cost, error in rows:
        model = model_id(name)  # rows from before OpenRouter ids say e.g. claude-sonnet-5-5
        a = agents.setdefault(agent, {"agent": agent, "models": [], "failed": [], "cost_usd": 0.0})
        if model not in a["models"]:
            a["models"].append(model)
        if error and model not in a["failed"]:
            a["failed"].append(model)
        a["cost_usd"] = round(a["cost_usd"] + (cost or 0), 4)
    return list(agents.values())


@router.get("/tasks/{task_id}")
async def get_task(task_id: int) -> dict[str, Any]:
    await worker.mark_stale_runs()
    try:
        t, r = await worker.load_task(task_id)
    except worker.TaskError as e:
        raise HTTPException(404, str(e)) from e
    res = t.result or {}
    pr = res.get("pr")
    return {
        "models": await _models_used(t.run_id),
        "chosen_models": t.plan.get("models") or {},
        "model_choices": await _model_choices() if t.status == "planned" else None,
        **_summary(t, r),
        "issue_url": t.issue.get("url"),
        "run_id": t.run_id,
        "base_commit": t.base_commit,
        "note": t.note,
        "plan_html": _md(t.plan.get("markdown", ""), r.full_name),
        "setup_commands": t.plan.get("setup_commands", []),
        "test_commands": t.plan.get("test_commands", []),
        "claim_comment": t.plan.get("claim_comment", ""),
        "actionable": t.plan.get("actionable") is not False,
        "claim_url": res.get("claim_url"),
        "baseline": res.get("baseline", []),
        "check": res.get("check"),
        "context_check": res.get("context_check"),
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
    note: str = Field(default="", max_length=4000)  # the developer's instruction to the Planner


@router.post("/tasks", status_code=202, dependencies=[Guard])
async def new_task(body: NewTask) -> dict[str, str]:
    """Checks the issue at once (exists, still free), then plans in the background."""
    try:
        current, _, _, moved = await worker.precheck(body.repo, body.issue)
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e
    body.repo = current
    planning = worker.plan_task(body.repo, body.issue, base=body.base or None, note=body.note)
    _background(f"plan-{body.repo}-{body.issue}", planning)
    return {"status": "planning", "repo": current, "note": moved or ""}


class Decision(BaseModel):
    approve: bool
    note: str | None = None
    setup_commands: list[str] = []
    test_commands: list[str] = []
    models: dict[str, str] = {}  # agent -> model to try first (router.CHOOSABLE)


@router.post("/tasks/{task_id}/decision", dependencies=[Guard])
async def decide(task_id: int, body: Decision) -> dict[str, Any]:
    try:
        await worker.decide(task_id, body.approve, body.note, models=body.models,
                            setup_commands=body.setup_commands, test_commands=body.test_commands)  # fmt: skip
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


@router.get("/tasks/{task_id}/feedback")
async def task_feedback(task_id: int) -> list[dict[str, Any]]:
    """The open review feedback on the task's PR (read from GitHub on request, not on every poll)."""
    try:
        return [c.model_dump() | {"suggested": c.suggested} for c in await worker.review_feedback(task_id)]
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e


class Address(BaseModel):
    comment_ids: list[str] = []
    note: str = Field(default="", max_length=4000)


@router.post("/tasks/{task_id}/address", dependencies=[Guard])
async def address(task_id: int, body: Address) -> dict[str, Any]:
    """Revise the task to address the chosen comments, and start the run."""
    try:
        await worker.address_review(task_id, body.comment_ids, body.note)
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e
    _background(f"task-{task_id}", worker.run_task(task_id))
    return await get_task(task_id)


class ReplyDraft(BaseModel):
    comment_ids: list[str]


@router.post("/tasks/{task_id}/replies/draft", dependencies=[Guard])
async def draft_replies(task_id: int, body: ReplyDraft) -> list[dict[str, Any]]:
    """Replies to comments that ask no code change, drafted without a run. Nothing is posted."""
    try:
        return await worker.draft_reply_only(task_id, body.comment_ids)
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e


class ReplyPost(BaseModel):
    replies: dict[str, str]


@router.post("/tasks/{task_id}/replies/post", dependencies=[Guard])
async def post_replies(task_id: int, body: ReplyPost) -> dict[str, list[str]]:
    """Post the approved replies with the operator's GitHub login."""
    try:
        return {"posted": await worker.post_reply_only(task_id, body.replies)}
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e


class FromPR(BaseModel):
    url: str = Field(max_length=300)


@router.post("/tasks/from-pr", dependencies=[Guard])
async def from_pr(body: FromPR) -> dict[str, Any]:
    """A task for a PR of yours that NightShift did not open, so its feedback can be addressed here."""
    try:
        task = await worker.task_from_pr(body.url)
    except (worker.TaskError, PRCheckError, PublishError) as e:
        raise HTTPException(409, str(e)) from e
    return await get_task(task.id)


class Attach(BaseModel):
    url: str = Field(max_length=300)


@router.post("/tasks/{task_id}/attach", dependencies=[Guard])
async def attach(task_id: int, body: Attach) -> dict[str, Any]:
    """Link a PR opened by hand: the task becomes published, and revisions follow up on that PR."""
    try:
        await worker.attach_pr(task_id, body.url)
    except worker.TaskError as e:
        raise HTTPException(409, str(e)) from e
    return await get_task(task_id)


class Publish(BaseModel):
    confirm: bool = False
    title: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=60_000)
    commit_message: str | None = Field(default=None, max_length=10_000)
    attribution: str | None = Field(default=None, max_length=200)  # "" leaves it out
    branch: str | None = Field(default=None, max_length=120)
    remember: bool = False  # keep the attribution and branch prefix for this repo
    replies: dict[str, str] | None = None  # follow-up: the operator's edits to the drafted replies, by comment id
    post_replies: bool = False  # follow-up: post the replies after pushing


@router.post("/tasks/{task_id}/publish", dependencies=[Guard])
async def publish(task_id: int, body: Publish) -> dict[str, Any]:
    """Gate 2. Without confirm: freshness, a dry run on the current upstream branch, and exactly what
    would be published. Edits to the title, body, commit message, attribution or branch are kept."""
    edits = body.model_dump(include=set(worker.GATE2_EDITS), exclude_none=True)
    try:
        return await worker.gate2(
            task_id, confirm=body.confirm, edits=edits, remember=body.remember, replies=body.replies,
            post_replies=body.post_replies,
        )  # fmt: skip
    except (worker.TaskError, PublishError) as e:
        raise HTTPException(409, str(e)) from e
