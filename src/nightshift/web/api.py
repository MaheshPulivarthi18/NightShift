"""JSON API for repos, health reports, jobs, issue chat and the fleet controls. Tasks live in tasks_api."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from nightshift.chat import chat_turn, get_or_create_chat, load_messages
from nightshift.db import Control, Item, LLMCall, Repo, Report, Run, RunEvent, session_scope
from nightshift.health.graph import run_health_report
from nightshift.health.report import is_opportunity
from nightshift.llm import spend_summary
from nightshift.memory import get_or_create_repo
from nightshift.web.common import Guard, render_markdown

log = structlog.get_logger()
router = APIRouter(prefix="/api")
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
Role = Literal["contributor", "maintainer"]

# Report runs started by this process, so Stop can cancel them.
_runs: dict[str, asyncio.Task[Any]] = {}


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


async def _repo(full_name: str) -> Repo:
    async with session_scope() as s:
        repo = (await s.execute(select(Repo).where(Repo.full_name == full_name))).scalar_one_or_none()
    if repo is None:
        raise HTTPException(404, f"Not watching {full_name}")
    return repo


async def _run_cost(run_ids: list[str]) -> dict[str, float]:
    if not run_ids:
        return {}
    async with session_scope() as s:
        rows = await s.execute(
            select(LLMCall.run_id, func.sum(LLMCall.cost_usd))
            .where(LLMCall.run_id.in_(run_ids))
            .group_by(LLMCall.run_id)
        )
    return {rid: float(c or 0) for rid, c in rows.all() if rid is not None}


def _start(repo: str, run_id: str, resume: bool = False) -> None:
    async def job() -> None:
        try:
            await run_health_report(repo, run_id=run_id, resume=resume)
        except Exception as e:  # recorded on the run by run_health_report; keep the server alive
            log.warning("web_run_failed", repo=repo, run_id=run_id, error=str(e))
        finally:
            _runs.pop(run_id, None)

    _runs[run_id] = asyncio.create_task(job())


# ---------- overview and controls ----------


@router.get("/overview")
async def overview() -> dict[str, Any]:
    async with session_scope() as s:
        control = await s.get(Control, 1)
    return {"spend": await spend_summary(), "killed": bool(control and control.killed)}


class KillIn(BaseModel):
    killed: bool


@router.post("/control/kill", dependencies=[Guard])
async def set_kill(body: KillIn) -> dict[str, Any]:
    async with session_scope() as s:
        control = await s.get(Control, 1)
        if control is None:
            s.add(Control(id=1, killed=body.killed))
        else:
            control.killed = body.killed
    return await overview()


# ---------- repos and reports ----------


@router.get("/repos")
async def list_repos() -> list[dict[str, Any]]:
    async with session_scope() as s:
        repos = list((await s.execute(select(Repo).order_by(Repo.full_name))).scalars())
        runs = {r.repo_id: r for r in (await s.execute(select(Run).order_by(Run.started_at))).scalars()}
        reports = {r.repo_id: r for r in (await s.execute(select(Report).order_by(Report.created_at))).scalars()}
    out = []
    for repo in repos:
        rep, run = reports.get(repo.id), runs.get(repo.id)
        snap = rep.findings if rep else {}
        out.append(
            {
                "full_name": repo.full_name,
                "role": repo.role,
                "verdict": (snap.get("pulse") or {}).get("verdict"),
                "opportunities": sum(1 for o in snap.get("opportunities", []) if is_opportunity(o)),
                "findings": len(snap.get("findings", [])) if repo.role == "maintainer" else None,
                "report_at": _iso(rep.created_at) if rep else None,
                "run": {"run_id": run.run_id, "status": run.status, "started_at": _iso(run.started_at)}
                if run
                else None,
            }
        )
    return out


class RepoIn(BaseModel):
    full_name: str
    role: Role = "contributor"


@router.post("/repos", dependencies=[Guard])
async def add_repo(body: RepoIn) -> list[dict[str, Any]]:
    name = body.full_name.strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
    if not REPO_RE.match(name):
        raise HTTPException(400, "Give owner/name or a GitHub URL.")
    async with session_scope() as s:
        await get_or_create_repo(s, name, role=body.role)
    return await list_repos()


class RoleIn(BaseModel):
    role: Role


@router.post("/repos/{owner}/{name}/role", dependencies=[Guard])
async def set_role(owner: str, name: str, body: RoleIn) -> list[dict[str, Any]]:
    repo = await _repo(f"{owner}/{name}")
    async with session_scope() as s:
        stored = await s.get(Repo, repo.id)
        assert stored is not None
        stored.role = body.role
    return await list_repos()


@router.post("/repos/{owner}/{name}/run", dependencies=[Guard])
async def start_run(owner: str, name: str) -> dict[str, str]:
    repo = await _repo(f"{owner}/{name}")
    run_id = uuid.uuid4().hex
    _start(repo.full_name, run_id)
    return {"run_id": run_id}


@router.get("/repos/{owner}/{name}/report")
async def report(owner: str, name: str, run_id: str | None = None) -> dict[str, Any]:
    repo = await _repo(f"{owner}/{name}")
    async with session_scope() as s:
        reports = list(
            (
                await s.execute(
                    select(Report).where(Report.repo_id == repo.id).order_by(Report.created_at.desc()).limit(20)
                )
            ).scalars()
        )
    if not reports:
        raise HTTPException(404, "No report yet. Run one from Repos.")
    current = next((r for r in reports if r.run_id == run_id), reports[0])
    return {
        "repo": repo.full_name,
        "role": repo.role,
        "run_id": current.run_id,
        "created_at": _iso(current.created_at),
        "cost_usd": round(current.cost_usd, 4),
        "html": render_markdown(current.markdown, repo.full_name),
        "history": [{"run_id": r.run_id, "created_at": _iso(r.created_at)} for r in reports],
    }


# ---------- jobs ----------


@router.get("/jobs")
async def list_jobs() -> list[dict[str, Any]]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Run, Repo).join(Repo, Repo.id == Run.repo_id).order_by(Run.started_at.desc()).limit(60)
            )
        ).all()
    costs = await _run_cost([run.run_id for run, _ in rows])
    return [
        {
            "run_id": run.run_id,
            "repo": repo.full_name,
            "kind": "task" if run.run_id.startswith("task-") else "map" if run.run_id.startswith("map-") else "report",
            "status": run.status,
            "node": run.current_node,
            "cost_usd": round(costs.get(run.run_id, 0.0), 4),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
        }
        for run, repo in rows
    ]


@router.get("/jobs/{run_id}")
async def get_job(run_id: str) -> dict[str, Any]:
    async with session_scope() as s:
        found = (
            await s.execute(select(Run, Repo).join(Repo, Repo.id == Run.repo_id).where(Run.run_id == run_id))
        ).first()
    if found is None:
        raise HTTPException(404, "No such run")
    run, repo = found
    return {
        "run_id": run.run_id,
        "repo": repo.full_name,
        "role": repo.role,
        "status": run.status,
        "node": run.current_node,
        "error": run.error,
        "has_report": bool(run.report_path),
        "cost_usd": round((await _run_cost([run_id])).get(run_id, 0.0), 4),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "stoppable": run_id in _runs,
    }


def _event_json(ev: RunEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "kind": ev.kind,
        "node": ev.node,
        "message": ev.message,
        "data": ev.data,
        "at": ev.created_at.astimezone(UTC).strftime("%H:%M:%S"),
    }


@router.get("/jobs/{run_id}/events")
async def job_events(run_id: str, after: int = 0) -> StreamingResponse:
    """Server-sent events: new run events as they are written, then the final status."""

    async def stream() -> AsyncIterator[str]:
        last, idle_after_finish = after, 0
        while True:
            async with session_scope() as s:
                evs = list(
                    (
                        await s.execute(
                            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.id > last).order_by(RunEvent.id)
                        )
                    ).scalars()
                )
                run = await s.get(Run, run_id)
            for ev in evs:
                last = ev.id
                yield f"event: step\ndata: {json.dumps(_event_json(ev))}\n\n"
            cost = (await _run_cost([run_id])).get(run_id, 0.0)
            status = {"status": run.status if run else "unknown", "node": run.current_node if run else None,
                      "cost_usd": round(cost, 4), "error": run.error if run else None}  # fmt: skip
            yield f"event: status\ndata: {json.dumps(status)}\n\n"
            if run is None or run.status != "running":
                idle_after_finish += 1
                if idle_after_finish >= 2:
                    yield "event: end\ndata: {}\n\n"
                    return
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/jobs/{run_id}/stop", dependencies=[Guard])
async def stop_job(run_id: str) -> dict[str, Any]:
    task = _runs.get(run_id)
    if task is None:
        raise HTTPException(409, "This run was not started by this server, or already finished.")
    task.cancel()
    return await get_job(run_id)


@router.post("/jobs/{run_id}/resume", dependencies=[Guard])
async def resume_job(run_id: str) -> dict[str, Any]:
    job = await get_job(run_id)
    if job["status"] == "running":
        raise HTTPException(409, "Already running")
    if not run_id.isalnum() or run_id.startswith(("task-", "map-")):
        raise HTTPException(409, "Only report runs can be resumed here.")
    _start(job["repo"], run_id, resume=True)
    return {**job, "status": "running"}


# ---------- chat ----------


async def _item(repo: Repo, number: int) -> Item:
    async with session_scope() as s:
        item = (
            await s.execute(select(Item).where(Item.repo_id == repo.id, Item.number == number))
        ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, f"#{number} is not stored for {repo.full_name}. Run a report first.")
    return item


def _tool_arg(args: dict[str, Any]) -> str:
    return str(args.get("path") or args.get("query") or args.get("prefix") or args.get("number") or "")


def _history(messages: list[Any], repo: str) -> list[dict[str, Any]]:
    """Chat turns for display: user text, assistant text (rendered), and which tools ran."""
    out: list[dict[str, Any]] = []
    for m in messages:
        texts = [b["text"] for b in m.content if b.get("type") == "text"]
        tools = [f"{b['name']} {_tool_arg(b.get('input', {}))}" for b in m.content if b.get("type") == "tool_use"]
        if m.role == "user" and texts:
            out.append({"role": "user", "text": "\n".join(texts)})
        elif m.role == "assistant":
            if tools:
                out.append({"role": "tools", "tools": tools})
            if texts:
                out.append({"role": "assistant", "html": render_markdown("\n\n".join(texts), repo)})
    return out


@router.get("/chat/{owner}/{name}/{number}")
async def chat(owner: str, name: str, number: int) -> dict[str, Any]:
    repo = await _repo(f"{owner}/{name}")
    item = await _item(repo, number)
    conv = await get_or_create_chat(repo, number)
    async with session_scope() as s:
        spent = (
            await s.execute(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.run_id == f"chat:{conv.id}")
            )
        ).scalar_one()
    return {
        "repo": repo.full_name,
        "item": {
            "number": item.number,
            "kind": item.kind,
            "title": item.title,
            "state": item.state,
            "url": item.url,
            "author": item.author,
            "labels": item.labels,
        },  # fmt: skip
        "body_html": render_markdown(item.body[:3000], repo.full_name),
        "history": _history(await load_messages(conv.id), repo.full_name),
        "cost_usd": round(float(spent), 4),
    }


class ChatIn(BaseModel):
    message: str


@router.post("/chat/{owner}/{name}/{number}", dependencies=[Guard])
async def chat_send(owner: str, name: str, number: int, body: ChatIn) -> StreamingResponse:
    """Streams the turn as newline-delimited JSON events (text, tool, error, done)."""
    repo = await _repo(f"{owner}/{name}")
    item = await _item(repo, number)
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Empty message")

    async def stream() -> AsyncIterator[str]:
        async for ev in chat_turn(repo, item, text[:8000]):
            yield json.dumps(ev) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")
