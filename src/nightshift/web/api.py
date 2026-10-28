"""JSON API for repos, health reports, jobs, issue chat and the fleet controls. Tasks live in tasks_api."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from nightshift import outcomes
from nightshift.catalog import ModelConfigError
from nightshift.chat import chat_turn, get_or_create_chat, load_messages
from nightshift.config import get_settings
from nightshift.db import Control, Item, LLMCall, PRCheck, Repo, RepoContext, Report, Run, RunEvent, session_scope
from nightshift.github import GitHubClient, GitHubError
from nightshift.health.graph import run_health_report
from nightshift.health.report import is_opportunity
from nightshift.llm import spend_summary
from nightshift.memory import content_hash, get_or_create_repo
from nightshift.router import AGENTS, agent_routes
from nightshift.web.common import Guard, model_view, render_markdown
from nightshift.worker.issue import fetch_issue
from nightshift.worker.onboard import onboard
from nightshift.worker.prcheck import PRCheckError, check_pr, fetch_pull, prcheck_run_id
from nightshift.worker.setup_agent import ENVIRONMENT, Environment, setup_repo, setup_run_id

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


def _start_onboard(repo: str, run_id: str) -> None:
    """Set a repo up in the background: environment, map and brief (worker/onboard.py)."""

    async def job() -> None:
        try:
            await onboard(repo, run_id)
        except Exception as e:  # recorded on the run by onboard
            log.warning("web_onboard_failed", repo=repo, run_id=run_id, error=str(e))
        finally:
            _runs.pop(run_id, None)

    _runs[run_id] = asyncio.create_task(job())


def _start_setup(repo: str, run_id: str) -> None:
    async def job() -> None:
        try:
            await setup_repo(repo, run_id)
        except Exception as e:  # recorded on the run by setup_repo
            log.warning("web_setup_failed", repo=repo, run_id=run_id, error=str(e))
        finally:
            _runs.pop(run_id, None)

    _runs[run_id] = asyncio.create_task(job())


def _job_kind(run_id: str) -> str:
    for prefix in ("task", "map", "setup", "prcheck"):
        if run_id.startswith(f"{prefix}-"):
            return prefix
    return "report"


def _environment_view(env: Environment, at: datetime) -> dict[str, Any]:
    return {
        "status": env.status,
        "commit": env.commit,
        "at": _iso(at),
        "detail": ": ".join(filter(None, [env.blocker, env.detail])),
        "setup_commands": env.setup_commands,
        "tests": [{"command": t.command, "ok": t.ok} for t in env.tests],
        "system_packages": env.system_packages,
        "tools": env.tools,
        "sources": env.sources,
        "attempts": env.attempts,
        "cost_usd": env.cost_usd,
    }


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
        repos = list((await s.execute(select(Repo).where(Repo.watched).order_by(Repo.full_name))).scalars())
        all_runs = list((await s.execute(select(Run).order_by(Run.started_at))).scalars())
        runs = {r.repo_id: r for r in all_runs if _job_kind(r.run_id) == "report"}
        setups = {r.repo_id: r for r in all_runs if _job_kind(r.run_id) == "setup"}
        envs = {
            c.repo_id: c
            for c in (
                await s.execute(
                    select(RepoContext).where(RepoContext.kind == ENVIRONMENT).order_by(RepoContext.created_at)
                )
            ).scalars()
        }
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
                "environment": _environment_view(Environment.model_validate(env.data), env.created_at)
                if (env := envs.get(repo.id))
                else None,
                "setup_run": {"run_id": su.run_id, "status": su.status} if (su := setups.get(repo.id)) else None,
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
        repo = await get_or_create_repo(s, name, role=body.role)
        repo.watched = True  # adding a removed repo again brings it and its history back
        repo_id = repo.id
    if not any(r.startswith(f"setup-{repo_id}-") for r in _runs):
        _start_onboard(name, setup_run_id(repo_id))  # learn the repo once: environment, map and brief
    return await list_repos()


@router.post("/repos/{owner}/{name}/onboard", dependencies=[Guard])
async def start_onboard(owner: str, name: str) -> dict[str, str]:
    """Set the repo up again: environment, map and brief, at the head of its default branch."""
    repo = await _repo(f"{owner}/{name}")
    if any(r.startswith(f"setup-{repo.id}-") for r in _runs):
        raise HTTPException(409, "This repo is already being set up.")
    run_id = setup_run_id(repo.id)
    _start_onboard(repo.full_name, run_id)
    return {"run_id": run_id}


@router.post("/repos/{owner}/{name}/remove", dependencies=[Guard])
async def remove_repo(owner: str, name: str) -> list[dict[str, Any]]:
    """Take a repo off the watchlist. Its reports, tasks and history stay; adding it again restores them."""
    repo = await _repo(f"{owner}/{name}")
    async with session_scope() as s:
        stored = await s.get(Repo, repo.id)
        assert stored is not None
        stored.watched = False
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


@router.post("/repos/{owner}/{name}/setup", dependencies=[Guard])
async def start_setup(owner: str, name: str) -> dict[str, str]:
    """Run the Setup agent again for this repo, at the head of its default branch."""
    repo = await _repo(f"{owner}/{name}")
    if any(r.startswith(f"setup-{repo.id}-") for r in _runs):
        raise HTTPException(409, "The Setup agent is already running for this repo.")
    run_id = setup_run_id(repo.id)
    _start_setup(repo.full_name, run_id)
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


LIVE_CHECK_LIMIT = 15  # the report's top picks; each costs a few GitHub calls, one of them a search
LIVE_CHECK_SECONDS = 600
_live: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}


@router.get("/repos/{owner}/{name}/report/live")
async def report_live(owner: str, name: str, run_id: str | None = None) -> dict[str, Any]:
    """Asks GitHub again about the issues a report lists as free: open, unassigned, and no open or merged
    PR references them (the three sources planning uses). Reports go stale; this does not."""
    repo = await _repo(f"{owner}/{name}")
    async with session_scope() as s:
        q = select(Report).where(Report.repo_id == repo.id)
        q = q.where(Report.run_id == run_id) if run_id else q.order_by(Report.created_at.desc()).limit(1)
        current = (await s.execute(q)).scalars().first()
    if current is None:
        raise HTTPException(404, "No report yet.")
    key = (repo.id, current.run_id)
    if (hit := _live.get(key)) and time.monotonic() - hit[0] < LIVE_CHECK_SECONDS:
        return hit[1]
    numbers = [o["number"] for o in (current.findings or {}).get("opportunities", []) if is_opportunity(o)]
    login = get_settings().github_login or None
    gate = asyncio.Semaphore(2)  # GitHub's search API refuses bursts

    async def check(number: int) -> dict[str, Any] | None:
        async with gate:
            try:
                why = (await fetch_issue(repo.full_name, number)).blockers(login)
            except GitHubError:
                return None
        return {"number": number, "why": "; ".join(why)} if why else None

    found = await asyncio.gather(*(check(n) for n in numbers[:LIVE_CHECK_LIMIT]))
    out = {
        "checked": min(len(numbers), LIVE_CHECK_LIMIT),
        "taken": [t for t in found if t],
        "at": _iso(datetime.now(UTC)),
    }
    _live[key] = (time.monotonic(), out)
    return out


# ---------- jobs ----------


# ---------- PR checks ----------


def _check_view(c: PRCheck, repo: Repo, run: Run | None) -> dict[str, Any]:
    status = run.status if c.status == "running" and run and run.status != "running" else c.status
    return {
        "id": c.id, "repo": repo.full_name, "number": c.number, "url": c.url, "title": c.title,
        "author": c.author, "status": status, "verdict": c.verdict, "must": c.must, "should": c.should,
        "cost_usd": round(c.cost_usd, 4), "run_id": c.run_id, "created_at": _iso(c.created_at), "error": c.error,
    }  # fmt: skip


@router.get("/pr-checks")
async def list_pr_checks() -> list[dict[str, Any]]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(PRCheck, Repo, Run)
                .join(Repo, Repo.id == PRCheck.repo_id)
                .outerjoin(Run, Run.run_id == PRCheck.run_id)
                .order_by(PRCheck.created_at.desc())
                .limit(100)
            )
        ).all()
    return [_check_view(c, repo, run) for c, repo, run in rows]


@router.get("/pr-checks/{check_id}")
async def get_pr_check(check_id: int) -> dict[str, Any]:
    async with session_scope() as s:
        row = (
            await s.execute(
                select(PRCheck, Repo, Run)
                .join(Repo, Repo.id == PRCheck.repo_id)
                .outerjoin(Run, Run.run_id == PRCheck.run_id)
                .where(PRCheck.id == check_id)
            )
        ).first()
    if row is None:
        raise HTTPException(404, "No such PR check")
    c, repo, run = row
    body = re.sub(r"\A# PR check:[^\n]*\n+", "", c.markdown)  # the page header already shows the PR's title
    return _check_view(c, repo, run) | {"html": render_markdown(body, repo.full_name)}


class CheckIn(BaseModel):
    url: str


@router.post("/pr-checks", dependencies=[Guard])
async def start_pr_check(body: CheckIn) -> dict[str, str]:
    """Check a pull request in the background; the PR is read first, so a bad URL fails at once."""
    try:
        pr = await fetch_pull(body.url)
    except PRCheckError as e:
        raise HTTPException(400, str(e)) from e
    async with session_scope() as s:
        repo = await get_or_create_repo(s, pr.repo)
    run_id = prcheck_run_id(repo.id, pr.number)

    async def job() -> None:
        try:
            await check_pr(pr.url, run_id)
        except Exception as e:  # recorded on the check and the run
            log.warning("web_pr_check_failed", url=pr.url, error=str(e))
        finally:
            _runs.pop(run_id, None)

    _runs[run_id] = asyncio.create_task(job())
    return {"run_id": run_id}


# ---------- models ----------


@router.get("/models")
async def models_page() -> dict[str, Any]:
    """Each agent's models in the order the router uses now, why, and how each has done."""
    agents = []
    for agent, (field, tools) in AGENTS.items():
        cfg = getattr(get_settings(), field)
        row: dict[str, Any] = {"agent": agent, "tools": tools, "effort": cfg.effort, "configured": cfg.models}
        try:
            st = await outcomes.stats(agent)
            row["routes"] = [
                {**model_view(r.info), "why": r.why, "stats": vars(st.get(r.info.id) or outcomes.ModelStats())}
                for r in await agent_routes(agent)
            ]
        except ModelConfigError as e:
            row["routes"], row["error"] = [], str(e)
        agents.append(row)
    return {"learning": get_settings().router_learning, "agents": agents}


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
            "kind": _job_kind(run.run_id),
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


def _when(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


async def _item(repo: Repo, number: int) -> Item:
    """The issue or PR as stored by a report, or else fetched from GitHub now and stored, so any item can be
    chatted about without a report first."""
    async with session_scope() as s:
        item = (
            await s.execute(select(Item).where(Item.repo_id == repo.id, Item.number == number))
        ).scalar_one_or_none()
    if item is not None:
        return item
    gh = GitHubClient()
    try:
        raw = await gh.rest_get(f"/repos/{repo.full_name}/issues/{number}")
        comments = await gh.rest_get(f"/repos/{repo.full_name}/issues/{number}/comments?per_page=100")
    except GitHubError as e:
        raise HTTPException(404, f"Cannot read {repo.full_name}#{number}: {e}") from e
    finally:
        await gh.aclose()
    fetched = {
        "title": raw["title"], "body": raw.get("body") or "", "state": raw["state"].upper(),
        "state_reason": raw.get("state_reason"), "labels": [lb["name"] for lb in raw.get("labels", [])],
        "data": {"comments": [
            {"id": c["id"], "author": (c.get("user") or {}).get("login") or "ghost",
             "association": c.get("author_association"), "created_at": c["created_at"], "body": c.get("body") or ""}
            for c in comments
        ]},
    }  # fmt: skip
    item = Item(
        repo_id=repo.id, number=number, kind="pr" if "pull_request" in raw else "issue",
        author=(raw.get("user") or {}).get("login") or "ghost", url=raw["html_url"], content_hash=content_hash(fetched),
        created_at=_when(raw["created_at"]), updated_at=_when(raw["updated_at"]), **fetched,
    )  # fmt: skip
    async with session_scope() as s:
        s.add(item)
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
