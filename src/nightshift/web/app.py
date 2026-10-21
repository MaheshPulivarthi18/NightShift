"""NightShift web app: watchlist, live jobs, rendered reports, and chat about an issue or PR.

Local by default (`nightshift serve` binds 127.0.0.1) and without login, so do not expose it
publicly. Reports and chats render GitHub text, so every HTML fragment built from Markdown
is sanitized with nh3 before it reaches the page.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nh3
import structlog
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from pydantic import BaseModel
from sqlalchemy import func, select

from nightshift.chat import chat_turn, get_or_create_chat, load_messages
from nightshift.db import Control, Item, LLMCall, Repo, Report, Run, RunEvent, session_scope
from nightshift.health.graph import run_health_report
from nightshift.health.report import is_opportunity
from nightshift.llm import spend_summary
from nightshift.memory import get_or_create_repo

log = structlog.get_logger()
HERE = Path(__file__).parent
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

app = FastAPI(title="NightShift")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
_md = MarkdownIt("commonmark", {"html": True}).enable("table")

# Runs started by this process, so the Stop button can cancel them.
_tasks: dict[str, asyncio.Task[Any]] = {}


def render_markdown(text: str, repo: str | None = None) -> str:
    """Markdown to sanitized HTML. Links to the repo's issues and PRs get a chat link next to them."""
    html = nh3.clean(_md.render(text), link_rel="noopener noreferrer")
    if repo:
        pattern = re.compile(rf'(<a href="https://github\.com/{re.escape(repo)}/(?:issues|pull)/(\d+)"[^>]*>[^<]*</a>)')
        html = pattern.sub(rf'\1 <a class="chat-link" href="/chat/{repo}/\2" title="Chat about #\2">chat</a>', html)
    return html


async def _layout_context() -> dict[str, Any]:
    async with session_scope() as s:
        control = await s.get(Control, 1)
    return {"spend": await spend_summary(), "killed": bool(control and control.killed)}


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
            _tasks.pop(run_id, None)

    _tasks[run_id] = asyncio.create_task(job())


# ---------- pages ----------


@app.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/repos")


@app.get("/repos", response_class=HTMLResponse)
async def repos_page(request: Request) -> HTMLResponse:
    async with session_scope() as s:
        repos = list((await s.execute(select(Repo).order_by(Repo.full_name))).scalars())
        latest_runs = {r.repo_id: r for r in (await s.execute(select(Run).order_by(Run.started_at))).scalars()}
        reports = {r.repo_id: r for r in (await s.execute(select(Report).order_by(Report.created_at))).scalars()}
    rows = []
    for repo in repos:
        rep = reports.get(repo.id)
        snap = rep.findings if rep else {}
        rows.append(
            {
                "repo": repo,
                "run": latest_runs.get(repo.id),
                "report": rep,
                "verdict": (snap.get("pulse") or {}).get("verdict"),
                "opportunities": sum(1 for o in snap.get("opportunities", []) if is_opportunity(o)),
                "findings": len(snap.get("findings", [])) if repo.role == "maintainer" else None,
            }
        )
    return templates.TemplateResponse(request, "repos.html", {"rows": rows, **await _layout_context()})


@app.post("/repos")
async def add_repo(full_name: str = Form(...), role: str = Form("contributor")) -> RedirectResponse:
    name = full_name.strip().removeprefix("https://github.com/").strip("/")
    if not REPO_RE.match(name) or role not in ("contributor", "maintainer"):
        raise HTTPException(400, "Give owner/name and a role of contributor or maintainer.")
    async with session_scope() as s:
        await get_or_create_repo(s, name, role=role)
    return RedirectResponse("/repos", status_code=303)


@app.post("/repos/{owner}/{name}/role")
async def set_role(owner: str, name: str, role: str = Form(...)) -> RedirectResponse:
    repo = await _repo(f"{owner}/{name}")
    if role not in ("contributor", "maintainer"):
        raise HTTPException(400, "Unknown role")
    async with session_scope() as s:
        stored = await s.get(Repo, repo.id)
        assert stored is not None
        stored.role = role
    return RedirectResponse("/repos", status_code=303)


@app.post("/repos/{owner}/{name}/run")
async def start_run(owner: str, name: str) -> RedirectResponse:
    repo = await _repo(f"{owner}/{name}")
    run_id = uuid.uuid4().hex
    _start(repo.full_name, run_id)
    return RedirectResponse(f"/jobs/{run_id}", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "jobs.html", await _layout_context())


@app.get("/jobs/table", response_class=HTMLResponse)
async def jobs_table(request: Request) -> HTMLResponse:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Run, Repo).join(Repo, Repo.id == Run.repo_id).order_by(Run.started_at.desc()).limit(50)
            )
        ).all()
    costs = await _run_cost([run.run_id for run, _ in rows])
    return templates.TemplateResponse(
        request, "_jobs_table.html", {"rows": rows, "costs": costs, "now": datetime.now(UTC)}
    )


@app.get("/jobs/{run_id}", response_class=HTMLResponse)
async def job_page(request: Request, run_id: str) -> HTMLResponse:
    async with session_scope() as s:
        found = (
            await s.execute(select(Run, Repo).join(Repo, Repo.id == Run.repo_id).where(Run.run_id == run_id))
        ).first()
    if found is None:
        raise HTTPException(404, "No such run")
    run, repo = found
    ctx = {"run": run, "repo": repo, "local": run_id in _tasks, **await _layout_context()}
    return templates.TemplateResponse(request, "job.html", ctx)


def _event_json(ev: RunEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "kind": ev.kind,
        "node": ev.node,
        "message": ev.message,
        "data": ev.data,
        "at": ev.created_at.astimezone(UTC).strftime("%H:%M:%S"),
    }


@app.get("/jobs/{run_id}/events")
async def job_events(run_id: str, after: int = 0) -> StreamingResponse:
    """Server-sent events: new run events as they are written, then the final status."""

    async def stream() -> AsyncIterator[str]:
        last = after
        idle_after_finish = 0
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
            status = {
                "status": run.status if run else "unknown",
                "node": run.current_node if run else None,
                "cost_usd": round(cost, 4),
                "error": run.error if run else None,
                "report": bool(run and run.report_path),
            }
            yield f"event: status\ndata: {json.dumps(status)}\n\n"
            if run is None or run.status != "running":
                idle_after_finish += 1
                if idle_after_finish >= 2:
                    yield "event: end\ndata: {}\n\n"
                    return
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/jobs/{run_id}/stop")
async def stop_job(run_id: str) -> RedirectResponse:
    task = _tasks.get(run_id)
    if task is None:
        raise HTTPException(
            409, "This run was not started by this server (or already finished); stop it where it runs."
        )
    task.cancel()
    return RedirectResponse(f"/jobs/{run_id}", status_code=303)


@app.post("/jobs/{run_id}/resume")
async def resume_job(run_id: str) -> RedirectResponse:
    async with session_scope() as s:
        found = (
            await s.execute(select(Run, Repo).join(Repo, Repo.id == Run.repo_id).where(Run.run_id == run_id))
        ).first()
    if found is None:
        raise HTTPException(404, "No such run")
    run, repo = found
    if run.status == "running":
        raise HTTPException(409, "Already running")
    _start(repo.full_name, run_id, resume=True)
    return RedirectResponse(f"/jobs/{run_id}", status_code=303)


@app.get("/repos/{owner}/{name}/report", response_class=HTMLResponse)
async def report_page(request: Request, owner: str, name: str, run_id: str | None = None) -> HTMLResponse:
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
        raise HTTPException(404, "No report yet. Run one from the Repos page.")
    current = next((r for r in reports if r.run_id == run_id), reports[0])
    ctx = {
        "repo": repo,
        "reports": reports,
        "current": current,
        "html": render_markdown(current.markdown, repo.full_name),
        **await _layout_context(),
    }
    return templates.TemplateResponse(request, "report.html", ctx)


async def _item(repo: Repo, number: int) -> Item:
    async with session_scope() as s:
        item = (
            await s.execute(select(Item).where(Item.repo_id == repo.id, Item.number == number))
        ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, f"#{number} is not stored for {repo.full_name}. Run a report first.")
    return item


def _history_view(messages: list[Any], repo: str) -> list[dict[str, Any]]:
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


def _tool_arg(args: dict[str, Any]) -> str:
    return str(args.get("path") or args.get("query") or args.get("prefix") or args.get("number") or "")


@app.get("/chat/{owner}/{name}/{number}", response_class=HTMLResponse)
async def chat_page(request: Request, owner: str, name: str, number: int) -> HTMLResponse:
    repo = await _repo(f"{owner}/{name}")
    item = await _item(repo, number)
    chat = await get_or_create_chat(repo, number)
    history = _history_view(await load_messages(chat.id), repo.full_name)
    async with session_scope() as s:
        spent = (
            await s.execute(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.run_id == f"chat:{chat.id}")
            )
        ).scalar_one()
    ctx = {
        "repo": repo,
        "item": item,
        "history": history,
        "chat_cost": float(spent),
        "body_html": render_markdown(item.body[:3000], repo.full_name),
        **await _layout_context(),
    }
    return templates.TemplateResponse(request, "chat.html", ctx)


@app.get("/chat/{owner}/{name}/{number}/history", response_class=HTMLResponse)
async def chat_history(request: Request, owner: str, name: str, number: int) -> HTMLResponse:
    repo = await _repo(f"{owner}/{name}")
    chat = await get_or_create_chat(repo, number)
    history = _history_view(await load_messages(chat.id), repo.full_name)
    return templates.TemplateResponse(request, "_chat_history.html", {"history": history})


class ChatIn(BaseModel):
    message: str


@app.post("/chat/{owner}/{name}/{number}")
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


@app.post("/control/kill")
async def toggle_kill() -> RedirectResponse:
    async with session_scope() as s:
        control = await s.get(Control, 1)
        if control is None:
            s.add(Control(id=1, killed=True))
        else:
            control.killed = not control.killed
    return RedirectResponse("/repos", status_code=303)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}
