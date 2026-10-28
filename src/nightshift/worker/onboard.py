"""Setting a repo up when it is added: what NightShift learns once and reuses for every issue and PR.

One run, in three steps, each shown on the run's log and kept per commit:

    environment  the Setup agent proves install and test commands in the sandbox (setup_agent.py)
    map          the architecture map: components, the files behind them, how they connect
    brief        the repo brief: what the project is, its PR rules, setup and test from its docs

A step that fails is recorded and the next one still runs: a repo whose tests cannot run in the sandbox
still gets a map and a brief.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from nightshift import events
from nightshift.config import get_settings
from nightshift.context import archmap
from nightshift.db import RepoContext, session_scope
from nightshift.health.graph import repo_brief
from nightshift.llm import LLMGateway
from nightshift.memory import get_or_create_repo
from nightshift.sandbox.docker import prepare_repo
from nightshift.worker.setup_agent import ensure_environment, setup_run_id

BRIEF = "brief"  # RepoContext kind: the repo brief as text, at the commit it was written for
MAP_AND_BRIEF_USD = 0.5  # on top of the Setup agent's own budget


async def onboard(full_name: str, run_id: str | None = None) -> str:
    """Set the repo up at the head of its default branch. Returns the run id."""
    async with session_scope() as s:
        repo = await get_or_create_repo(s, full_name)
    run_id = run_id or setup_run_id(repo.id)
    await events.start_run(run_id, repo.id)
    llm = LLMGateway(run_id, repo.id, run_budget_usd=get_settings().setup_budget_usd + MAP_AND_BRIEF_USD)
    failed: list[str] = []
    commit, step = "", "start"
    try:
        box, _ = await prepare_repo(full_name, "HEAD", [])
        async with box:
            commit = (await box.exec("git rev-parse HEAD")).stdout.strip()
            step = "environment"
            await events.record(run_id, "node_start", "environment", node="environment")
            env = await ensure_environment(llm, repo, box, commit)
            if env.status != "ready":
                failed.append(f"environment {env.status}: {env.blocker or env.detail}")
            step = "map"
            await events.record(run_id, "node_start", "map", node="map")
            try:
                await archmap.architecture_map(box, repo, llm)
            except Exception as e:  # a missing map leaves the rest of the repo's knowledge useful
                failed.append(f"map: {type(e).__name__}: {e}")
                await events.record(run_id, "warning", f"map failed: {e}")
        step = "brief"
        await events.record(run_id, "node_start", "brief", node="brief")
        brief, _ = await repo_brief(llm, full_name)
        if brief is None:
            failed.append("brief: the repo's docs could not be read")
        else:
            from nightshift.worker.task import brief_text  # the task module imports this package's agents

            await _save_brief(repo.id, commit, brief_text(brief))
    except asyncio.CancelledError:  # the server stopped (closed, restarted): say so, not "CancelledError"
        await asyncio.shield(events.finish_run(run_id, "interrupted", error=_interrupted(step)))
        raise
    except BaseException as e:
        await events.finish_run(run_id, "failed", error=f"{type(e).__name__}: {e}")
        raise
    await events.finish_run(run_id, "stopped" if failed else "done", error="; ".join(failed)[:1000] or None)
    return run_id


def _interrupted(step: str) -> str:
    return f"Interrupted: NightShift was closed during the {step} step. Set it up again to finish."


async def _save_brief(repo_id: int, commit: str, text: str) -> None:
    async with session_scope() as s:
        row = (
            await s.execute(
                select(RepoContext).where(
                    RepoContext.repo_id == repo_id, RepoContext.kind == BRIEF, RepoContext.commit == commit
                )
            )
        ).scalar_one_or_none()
        if row:
            row.data = {"text": text}
        else:
            s.add(RepoContext(repo_id=repo_id, kind=BRIEF, commit=commit, data={"text": text}))
