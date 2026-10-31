"""Command line entry point: `nightshift <command>`."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
import uuid
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any, Literal

import structlog
import typer
from dotenv import load_dotenv
from sqlalchemy import select

from nightshift import events
from nightshift.backup import BackupError, backup
from nightshift.catalog import ModelConfigError
from nightshift.config import get_settings
from nightshift.db import Control, Feedback, Repo, Report, session_scope
from nightshift.github import GitHubError
from nightshift.llm import BudgetExceeded, KillSwitchActive, ModelAPIError, spend_summary
from nightshift.memory import get_or_create_repo, purge_repo

app = typer.Typer(help="NightShift: autonomous repo maintainer agents.", no_args_is_help=True)
watch_app = typer.Typer(help="Manage the watchlist of public repos.", no_args_is_help=True)
report_app = typer.Typer(help="Repo Health Reports.", no_args_is_help=True)
sandbox_app = typer.Typer(help="Docker sandboxes where repo code runs.", no_args_is_help=True)
context_app = typer.Typer(
    help="Repo context for agents: architecture map (L1) and change context (L3).", no_args_is_help=True
)
task_app = typer.Typer(
    help="Contribution tasks: plan an issue, approve, fix and review in a sandbox.", no_args_is_help=True
)
app.add_typer(watch_app, name="watch")
app.add_typer(report_app, name="report")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(context_app, name="context")
app.add_typer(task_app, name="task")

log = structlog.get_logger()

REPO_RE = re.compile(r"(?:https?://github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=logging.WARNING)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(level))


def _repo_name(value: str) -> str:
    m = REPO_RE.match(value.strip())
    if not m:
        raise typer.BadParameter(f"Not a GitHub repo: {value}")
    return m.group(1)


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    load_dotenv()  # so the Anthropic SDK sees ANTHROPIC_API_KEY from .env
    _setup_logging(verbose)


# ---------- watchlist ----------


Role = Literal["contributor", "maintainer"]


@watch_app.command("add")
def watch_add(
    repos: list[str],
    role: Annotated[Role, typer.Option(help="contributor: where you can help. maintainer: backlog triage.")] = (
        "contributor"
    ),
) -> None:
    """Add public repos (owner/name or URL) to the watchlist."""

    async def run() -> None:
        async with session_scope() as s:
            count = len((await s.execute(select(Repo).where(Repo.connected.is_(False), Repo.watched))).scalars().all())
            for raw in repos:
                name = _repo_name(raw)
                exists = (await s.execute(select(Repo).where(Repo.full_name == name))).scalar_one_or_none()
                if exists and exists.watched:
                    typer.echo(f"Already watching {name}")
                    continue
                if exists:  # removed earlier: back on the watchlist, with its history
                    exists.watched = True
                    count += 1
                    typer.echo(f"Watching {name} again")
                    continue
                if count >= (cap := get_settings().watchlist_max):
                    typer.echo(f"Watchlist is full ({cap}; NIGHTSHIFT_WATCHLIST_MAX raises it). Skipping {name}.")
                    continue
                await get_or_create_repo(s, name, role=role)
                count += 1
                typer.echo(f"Watching {name} as {role}")

    asyncio.run(run())


@watch_app.command("list")
def watch_list() -> None:
    """Show watched and connected repos."""

    async def run() -> None:
        async with session_scope() as s:
            repos = (await s.execute(select(Repo).order_by(Repo.full_name))).scalars().all()
        for r in repos:
            kind = "connected" if r.connected else "watched"
            fetched = f"{r.last_fetched_at:%Y-%m-%d %H:%M}" if r.last_fetched_at else "never"
            flags = ("  (paused)" if r.paused else "") + ("" if r.watched else "  (removed)")
            typer.echo(f"{r.full_name:45} {kind:10} {r.role:12} last fetched: {fetched}{flags}")

    asyncio.run(run())


@watch_app.command("role")
def watch_role(repo: str, role: Role) -> None:
    """Set which report a repo gets: contributor or maintainer."""

    async def run() -> None:
        name = _repo_name(repo)
        async with session_scope() as s:
            r = (await s.execute(select(Repo).where(Repo.full_name == name))).scalar_one_or_none()
            if r is None:
                typer.echo(f"Not watching {name}")
                return
            r.role = role
        typer.echo(f"{name} now gets the {role} report")

    asyncio.run(run())


@watch_app.command("remove")
def watch_remove(
    repo: str,
    purge: Annotated[
        bool, typer.Option("--purge", help="Delete everything stored about it: reports, tasks, history.")
    ] = False,
    no_backup: Annotated[bool, typer.Option("--no-backup", help="Purge without dumping the database first.")] = False,
) -> None:
    """Take a repo off the watchlist, keeping its history (adding it again restores it); --purge deletes it all,
    after a backup of the database."""

    async def run() -> None:
        name = _repo_name(repo)
        async with session_scope() as s:
            r = (await s.execute(select(Repo).where(Repo.full_name == name))).scalar_one_or_none()
            if r is None:
                typer.echo(f"Not watching {name}")
                return
            if purge and not no_backup:
                try:
                    typer.echo(f"Backed up the database first: {backup(f'purge {name}')}")
                except BackupError as e:
                    typer.echo(f"Not purged: the backup failed ({e}). Use --no-backup to purge anyway.", err=True)
                    raise typer.Exit(1) from e
            if purge:
                await purge_repo(s, r)
            else:
                r.watched = False
        typer.echo(f"{'Deleted' if purge else 'Removed from the watchlist (history kept)'}: {name}")

    asyncio.run(run())


# ---------- reports ----------


@report_app.command("run")
def report_run(
    repo: Annotated[str | None, typer.Argument(help="owner/name or URL. Omit with --all.")] = None,
    all_repos: Annotated[bool, typer.Option("--all", help="Run for every repo on the watchlist.")] = False,
    resume: Annotated[str | None, typer.Option(help="Resume a stopped run by its run id.")] = None,
) -> None:
    """Build a health report (fetch, link, analyse, assess, verify, render)."""
    from nightshift.health.graph import run_health_report
    from nightshift.health.report import is_opportunity

    async def names() -> list[str]:
        if not all_repos:
            if repo is None:
                raise typer.BadParameter("Give a repo or --all.")
            return [_repo_name(repo)]
        async with session_scope() as s:
            rows = (
                await s.execute(select(Repo).where(Repo.paused.is_(False), Repo.watched).order_by(Repo.full_name))
            ).scalars()
            return [r.full_name for r in rows]

    async def run() -> None:
        for name in await names():
            run_id = resume or uuid.uuid4().hex
            try:
                state = await run_health_report(name, run_id=run_id, resume=bool(resume))
            except (BudgetExceeded, KillSwitchActive) as e:
                typer.echo(f"Stopped {name}: {e}\nResume later with: nightshift report run {name} --resume {run_id}")
                return
            except GitHubError as e:
                typer.echo(f"GitHub error for {name}: {e}", err=True)
                continue
            except ModelConfigError as e:
                typer.echo(f"Model setup problem: {e}", err=True)
                return
            except ModelAPIError as e:
                if e.status == 401:
                    typer.echo("OpenRouter key missing or invalid. Check OPENROUTER_API_KEY.", err=True)
                    return
                if e.status != 402:
                    raise
                typer.echo(
                    f"Model provider is out of credits (402): {e.message}\n"
                    f"Add credits, then resume with: nightshift report run {name} --resume {run_id}",
                    err=True,
                )
                return
            except Exception as e:  # one repo's failure must not stop the rest of --all
                if not all_repos:
                    raise
                log.exception("report_failed", repo=name)
                typer.echo(f"Report failed for {name}: {type(e).__name__}: {e}. Resume: --resume {run_id}", err=True)
                continue
            st = state.get("stats", {})
            if state.get("role") == "contributor":
                opps = [o for o in state.get("opportunities", []) if is_opportunity(o)]
                summary = f"{state.get('pulse', {}).get('verdict', '?')}, {len(opps)} open issues worth a look"
            else:
                summary = f"{st.get('findings_verified', 0)} findings, {len(state.get('clusters', []))} clusters"
            typer.echo(f"{name}: {summary}, ${st.get('cost_usd', 0):.2f} -> {state.get('report_path')}")

    asyncio.run(run())


@report_app.command("show")
def report_show(repo: str) -> None:
    """Print the latest report for a repo."""

    async def run() -> None:
        name = _repo_name(repo)
        async with session_scope() as s:
            r = (await s.execute(select(Repo).where(Repo.full_name == name))).scalar_one_or_none()
            if r is None:
                raise typer.BadParameter(f"Unknown repo {name}")
            rep = (
                await s.execute(
                    select(Report).where(Report.repo_id == r.id).order_by(Report.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
        typer.echo(rep.markdown if rep else "No report yet.")

    asyncio.run(run())


@report_app.command("feedback")
def report_feedback(
    repo: str,
    key: Annotated[str, typer.Argument(help="Finding key, e.g. close_candidate:#42")],
    correct: Annotated[bool, typer.Option("--correct/--wrong")],
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Mark a finding right or wrong. Stored as memory and used to calibrate future reports."""

    async def run() -> None:
        name = _repo_name(repo)
        async with session_scope() as s:
            r = (await s.execute(select(Repo).where(Repo.full_name == name))).scalar_one()
            rep = (
                await s.execute(
                    select(Report).where(Report.repo_id == r.id).order_by(Report.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            finding = next((f for f in (rep.findings.get("findings", []) if rep else []) if f["key"] == key), None)
            if finding is None:
                raise typer.BadParameter(f"No finding {key} in the latest report.")
            slim = {k: finding[k] for k in ("key", "type", "claim", "confidence")}
            s.add(Feedback(repo_id=r.id, finding_key=key, correct=correct, note=note, finding=slim))
        typer.echo("Saved.")

    asyncio.run(run())


# ---------- sandbox and context ----------

SetupOpt = Annotated[list[str] | None, typer.Option("--setup", help="Setup command, run in the repo dir. Repeatable.")]
RefOpt = Annotated[str, typer.Option(help="Commit or branch to check out.")]
FetchOpt = Annotated[str | None, typer.Option(help="Fetch --ref from this remote first, e.g. a fork's URL.")]
OutOpt = Annotated[Path, typer.Option(help="Folder for the output files.")]


def _out_dir(out: Path | None, name: str, what: str) -> Path:
    path = out or Path(get_settings().reports_dir) / name.replace("/", "__") / what
    path.mkdir(parents=True, exist_ok=True)
    return path


@sandbox_app.command("build")
def sandbox_build() -> None:
    """Build the base image task sandboxes start from."""
    from nightshift.sandbox.docker import INSIDE_DIR

    image = get_settings().sandbox_image
    dockerfile = INSIDE_DIR.parent / "Dockerfile"
    code = subprocess.run(["docker", "build", "-t", image, "-f", str(dockerfile), str(dockerfile.parent)]).returncode
    raise typer.Exit(code)


@sandbox_app.command("try")
def sandbox_try(
    repo: str,
    ref: RefOpt = "HEAD",
    setup: SetupOpt = None,
    test: Annotated[list[str] | None, typer.Option("--test", help="Test command. Repeatable.")] = None,
    fetch: FetchOpt = None,
) -> None:
    """Set a repo up in a fresh sandbox and run its tests: the baseline every task starts from."""
    from nightshift.sandbox.docker import SandboxError, prepare_repo

    async def run() -> bool:
        from nightshift.worker.task import save_recipe
        from nightshift.worker.verify import run_commands

        name = _repo_name(repo)
        box, cached = await prepare_repo(name, ref, setup or [], fetch_url=fetch)
        async with box:
            typer.echo(f"Sandbox ready ({'cached image' if cached else 'fresh setup, image saved'})")
            results = await run_commands(box, test or [])
            for r in results:
                status = "PASS" if r.ok else ("TIMEOUT" if r.timed_out else f"FAIL (exit {r.exit_code})")
                typer.echo(f"\n$ {r.command}\n{status} in {r.seconds:.0f}s\n{r.tail[-1500:]}")
            commit = (await box.exec("git rev-parse HEAD")).stdout.strip()
        if results and all(r.ok for r in results):  # commands that worked are offered to the next plan
            async with session_scope() as s:
                repo_row = await get_or_create_repo(s, name)
            await save_recipe(repo_row.id, commit, setup or [], results)
            typer.echo("\nSaved as this repo's setup and test recipe.")
        return all(r.ok for r in results)

    try:
        ok = asyncio.run(run())
    except SandboxError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    raise typer.Exit(0 if ok else 1)


@sandbox_app.command("setup")
def sandbox_setup(repo: str) -> None:
    """Let the Setup agent work out how to install and test a repo, and prove it in the sandbox."""
    from nightshift.worker.setup_agent import setup_repo

    async def run() -> None:
        env = await setup_repo(_repo_name(repo))
        for line in env.attempts:
            typer.echo(f"- {line}")
        typer.echo(f"\nEnvironment {env.status} (${env.cost_usd:.2f})")
        if env.status != "ready":
            typer.echo(": ".join(filter(None, [env.blocker, env.detail])))
            raise typer.Exit(1)
        typer.echo(f"Packages: {env.system_packages or 'none'}  Tools: {env.tools or 'none'}")
        typer.echo("Setup:\n" + "\n".join(f"  {c}" for c in env.setup_commands))
        typer.echo("Tests:\n" + "\n".join(f"  {t.line()}" for t in env.tests))

    _run_sandboxed(run())


@app.command("check-pr")
def check_pr_cmd(url: Annotated[str, typer.Argument(help="https://github.com/owner/repo/pull/123")]) -> None:
    """Check a pull request as a careful maintainer would: guidelines, evidence, run it, review it. Posts nothing."""
    from nightshift.worker.prcheck import PRCheckError, check_pr

    async def run() -> None:
        try:
            report = await check_pr(url)
        except PRCheckError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        typer.echo(report.markdown())

    _run_sandboxed(run())


@app.command("bench")
def bench_cmd(
    repo: Annotated[str, typer.Argument(help="owner/name")],
    merged: Annotated[int, typer.Option(help="How many merged PRs")] = 10,
    closed: Annotated[int, typer.Option(help="How many closed, unmerged PRs")] = 10,
    days: Annotated[int, typer.Option(help="Decided in the last N days")] = 90,
) -> None:
    """Score the PR checker on PRs maintainers already decided: its verdict against theirs. Posts nothing."""
    from nightshift.worker.benchmark import run_benchmark

    async def run() -> None:
        path = await run_benchmark(_repo_name(repo), merged, closed, days)
        typer.echo(path.read_text())

    _run_sandboxed(run())


@app.command("models")
def models_cmd(agent: Annotated[str | None, typer.Argument(help="One agent, e.g. coder")] = None) -> None:
    """Each agent's models in the order the router uses now, why, and how each has done."""
    from nightshift import outcomes
    from nightshift.router import AGENTS, agent_routes

    async def run() -> None:
        for name in [agent] if agent else list(AGENTS):
            if name not in AGENTS:
                typer.echo(f"Unknown agent {name}. Agents: {', '.join(AGENTS)}", err=True)
                raise typer.Exit(1)
            st = await outcomes.stats(name)
            typer.echo(f"\n{name}")
            for i, r in enumerate(await agent_routes(name), 1):
                s = st.get(r.info.id) or outcomes.ModelStats()
                tasks = f"tasks {s.tasks_text()} ok" if s.tasks else "no task outcomes"
                latency = f"{s.latency_ms / 1000:.1f}s avg" if s.latency_ms else "-"
                typer.echo(
                    f"  {i}. {r.info.id:34} {r.why}\n"
                    f"     {s.calls} calls, {s.failed_calls} failed · {tasks} · ${s.cost_usd:.2f} · {latency}"
                )

    asyncio.run(run())


def _run_sandboxed(coro: Coroutine[Any, Any, None]) -> None:
    """Run a sandbox command, turning sandbox and budget failures into a message and exit code 1."""
    from nightshift.sandbox.docker import SandboxError
    from nightshift.worker.publish import PublishError
    from nightshift.worker.task import TaskError

    try:
        asyncio.run(coro)
    except (SandboxError, KillSwitchActive, BudgetExceeded, PublishError, TaskError, ModelConfigError) as e:
        typer.echo(f"{type(e).__name__}: {e}", err=True)
        raise typer.Exit(1) from e
    except ModelAPIError as e:
        hint = (
            " Check OPENROUTER_API_KEY and the key's credit limit on openrouter.ai."
            if e.status in (401, 402, 403)
            else ""
        )
        typer.echo(f"Model provider error {e}.{hint}", err=True)
        raise typer.Exit(1) from e


@context_app.command("map")
def context_map(repo: str, ref: RefOpt = "HEAD", fetch: FetchOpt = None, out: OutOpt | None = None) -> None:
    """Build (or load) the L1 architecture map of a repo at a commit. One model call per new commit."""
    from nightshift.context import archmap
    from nightshift.llm import LLMGateway
    from nightshift.sandbox.docker import Sandbox, prepare_repo

    async def run() -> None:
        name = _repo_name(repo)
        async with session_scope() as s:
            r = await get_or_create_repo(s, name)
        run_id = f"map-{uuid.uuid4().hex[:12]}"
        await events.start_run(run_id, r.id)
        box: Sandbox | None = None
        try:
            box, _ = await prepare_repo(name, ref, [], fetch_url=fetch)
            data = await archmap.architecture_map(box, r, LLMGateway(run_id, r.id))
            await events.finish_run(run_id, "done")
        except BaseException as e:
            await events.finish_run(run_id, "failed", error=str(e))
            raise
        finally:
            if box:
                await box.remove()
        folder = _out_dir(out, name, "context")
        stem = f"architecture-{data['meta']['commit'][:7]}"
        (folder / f"{stem}.json").write_text(json.dumps(data, indent=1))
        (folder / f"{stem}.md").write_text(archmap.to_markdown(data))
        (folder / f"{stem}.html").write_text(archmap.to_html(name, data))
        typer.echo(json.dumps(data["meta"], indent=1))
        typer.echo(f"Written to {folder}/{stem}.(json|md|html)")

    _run_sandboxed(run())


@context_app.command("coverage")
def context_coverage() -> None:
    """For every task that ran: did its planning context hold the files the fix changed?"""
    from nightshift.db import Task

    async def run() -> None:
        async with session_scope() as s:
            rows = (await s.execute(select(Task, Repo).join(Repo).order_by(Task.id))).all()
        changed = relevant = planned = missed = 0
        for t, r in rows:
            if not (cc := (t.result or {}).get("context_check")):
                continue
            changed, relevant = changed + len(cc["changed"]), relevant + len(cc["relevant"])
            planned, missed = planned + len(cc["planned"]), missed + len(cc["missed"])
            typer.echo(f"{r.full_name}#{t.issue_number} (task {t.id}): {_context_line(cc)}")
        if not changed:
            typer.echo("No checked task yet: the check runs after a fix, for tasks planned from now on.")
            return
        typer.echo(
            f"\nAll tasks: {changed - missed} of {changed} changed files were in context "
            f"({relevant} in the relevant files, {planned} named in the plan); {missed} missed."
        )

    asyncio.run(run())


@context_app.command("change")
def context_change(
    repo: str,
    base: Annotated[str, typer.Option(help="Commit the change is compared with.")],
    ref: RefOpt = "",
    patch: Annotated[Path | None, typer.Option(help="A diff to apply on top of --base (instead of --ref).")] = None,
    fetch: FetchOpt = None,
    budget: Annotated[int, typer.Option(help="Token budget for the context pack.")] = 6000,
    out: OutOpt | None = None,
) -> None:
    """L3 change context for a change: what it touches, what calls it, and related code it did NOT change."""
    from nightshift.context.change import change_context, to_markdown
    from nightshift.sandbox.docker import prepare_repo

    if bool(ref) == bool(patch):
        raise typer.BadParameter("Give exactly one of --ref or --patch.")

    async def run() -> None:
        name = _repo_name(repo)
        started = time.monotonic()
        box, _ = await prepare_repo(name, ref or base, [], fetch_url=fetch)
        async with box:
            if patch:
                await box.write_file("/tmp/change.patch", patch.read_text())
                applied = await box.exec("git apply --whitespace=nowarn /tmp/change.patch")
                if not applied.ok:
                    raise typer.BadParameter(f"Patch does not apply on {base}: {applied.tail(800)}")
            ctx = await change_context(box, base)
        text, tokens = to_markdown(ctx, budget)
        folder = _out_dir(out, name, "context")
        label = ref or (patch.stem if patch else "")
        stem = f"change-{base[:7]}-{label[:20].replace('/', '_')}"
        (folder / f"{stem}.md").write_text(text)
        (folder / f"{stem}.json").write_text(ctx.model_dump_json(indent=1))
        typer.echo(f"{len(ctx.findings)} findings, ~{tokens} tokens, {time.monotonic() - started:.0f}s")
        typer.echo("Language servers: " + (", ".join(ctx.servers) or "none"))
        for err in ctx.errors:
            typer.echo(f"  warning: {err}")
        typer.echo("\nRelated code NOT changed by the diff:")
        for f in ctx.unchanged_related:
            typer.echo(f"- {f.path}:{f.line + 1} {f.symbol}: {f.reason}")
        typer.echo(f"\nWritten to {folder}/{stem}.(md|json)")

    _run_sandboxed(run())


# ---------- contribution tasks ----------


def _task_line(t: Any) -> str:
    return f"#{t.id:<4} {t.status:15} issue #{t.issue_number:<6} ${t.cost_usd:.2f}"


@task_app.command("plan")
def task_plan(
    repo: str,
    issue: int,
    base: Annotated[str | None, typer.Option(help="Commit to work from (default: the default branch).")] = None,
    note: Annotated[str, typer.Option(help='Your instruction to the Planner, e.g. "treat this as a docs task".')] = "",
) -> None:
    """Plan a fix for an issue. Stops at Gate 1: nothing runs until you approve."""
    from nightshift.worker.task import TaskError, plan_task

    async def run() -> None:
        try:
            task = await plan_task(_repo_name(repo), issue, base=base, note=note)
        except TaskError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        typer.echo(task.plan["markdown"])
        typer.echo(f"\nTask {task.id} planned for ${task.cost_usd:.2f}. Approve: nightshift task approve {task.id}")

    _run_sandboxed(run())


@task_app.command("show")
def task_show(task_id: int) -> None:
    """Show a task: its plan, and its verification and review once it has run."""
    from nightshift.worker.task import load_task

    task, repo = asyncio.run(load_task(task_id))
    typer.echo(f"{repo.full_name} {_task_line(task)}")
    if task.error:
        typer.echo(f"Stopped: {task.error}")
    typer.echo(task.plan.get("markdown", ""))
    for r in task.result.get("reviews", []):
        typer.echo("\n" + r["markdown"])
    if cc := task.result.get("context_check"):
        typer.echo("\n" + _context_line(cc))


def _context_line(cc: dict[str, list[str]]) -> str:
    held = len(cc["changed"]) - len(cc["missed"])
    line = f"Context check: planning context held {held} of {len(cc['changed'])} files the fix changed"
    return line + (f"; missed {', '.join(cc['missed'])}" if cc["missed"] else "")


@task_app.command("list")
def task_list() -> None:
    """All tasks, newest first. Archived ones are marked."""
    from nightshift.db import Task
    from nightshift.worker.task import mark_stale_runs

    async def run() -> None:
        await mark_stale_runs()
        async with session_scope() as s:
            rows = (await s.execute(select(Task, Repo).join(Repo).order_by(Task.id.desc()))).all()
        for t, r in rows:
            typer.echo(f"{r.full_name:35} {_task_line(t)}{'  (archived)' if t.archived_at else ''}")

    asyncio.run(run())


@task_app.command("attach")
def task_attach(task_id: int, url: str) -> None:
    """Link a PR you opened by hand to its task; later revisions push follow-up commits to it."""
    from nightshift.worker.task import TaskError, attach_pr

    try:
        asyncio.run(attach_pr(task_id, url))
    except TaskError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Task {task_id} is published as {url}.")


@task_app.command("feedback")
def task_feedback(task_id: int) -> None:
    """The open review feedback on a task's pull request: [x] marks what is worth addressing."""
    from nightshift.worker.task import TaskError, review_feedback

    try:
        comments = asyncio.run(review_feedback(task_id))
    except TaskError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    for c in comments:
        flags = ", ".join(f for f, on in (("bot", c.bot), ("outdated", c.outdated), ("superseded", c.superseded),
                                          ((c.state or "").lower(), bool(c.state))) if on)  # fmt: skip
        where = f" {c.path}:{c.line}" if c.path else ""
        typer.echo(f"{'[x]' if c.suggested else '[ ]'} {c.id}  @{c.author}{where}{f'  ({flags})' if flags else ''}")
        typer.echo("    " + c.body.splitlines()[0][:160] if c.body else "")
    if not comments:
        typer.echo("No open feedback.")


@task_app.command("address")
def task_address(
    task_id: int,
    comment: Annotated[
        list[str] | None, typer.Option("--comment", help="A comment id from `task feedback`. Repeatable.")
    ] = None,
    note: Annotated[str, typer.Option(help="Your own instruction, with or instead of comments.")] = "",
) -> None:
    """Address review feedback: the run starts from the PR as published; Gate 2 pushes a follow-up commit."""
    from nightshift.worker.task import TaskError, address_review, run_task

    async def run() -> None:
        try:
            await address_review(task_id, comment or [], note)
        except TaskError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        task = await run_task(task_id)
        typer.echo(f"Task {task_id} is {task.status}. Gate 2 pushes the follow-up commit and drafts the replies.")

    _run_sandboxed(run())


@task_app.command("from-pr")
def task_from_pr_cmd(url: str) -> None:
    """A task for a pull request of yours that NightShift did not open, to address its feedback here."""
    from nightshift.worker.task import TaskError, task_from_pr

    async def run() -> None:
        try:
            task = await task_from_pr(url)
        except TaskError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        typer.echo(f"Task {task.id} follows {url}. See its feedback: nightshift task feedback {task.id}")

    _run_sandboxed(run())


@task_app.command("archive")
def task_archive(
    task_id: int, undo: Annotated[bool, typer.Option("--undo", help="Put it back on the board.")] = False
) -> None:
    """Take a task off the board; it and its history are kept."""
    from nightshift.worker.task import TaskError, archive

    try:
        asyncio.run(archive(task_id, not undo))
    except TaskError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Task {task_id} {'is back on the board' if undo else 'archived'}.")


@task_app.command("approve")
def task_approve(
    task_id: int,
    note: Annotated[str | None, typer.Option()] = None,
    setup: SetupOpt = None,
    test: Annotated[list[str] | None, typer.Option("--test", help="Replace the plan's test commands.")] = None,
    model: Annotated[
        list[str] | None,
        typer.Option("--model", help="AGENT=MODEL to try first, e.g. coder=openai/gpt-5.6-sol. Repeatable."),
    ] = None,
) -> None:
    """Gate 1: approve a plan (optionally replacing its commands or choosing models)."""
    from nightshift.worker.task import TaskError, decide

    models = dict(m.split("=", 1) for m in model or [] if "=" in m)
    try:
        asyncio.run(decide(task_id, True, note, models=models, setup_commands=setup or [], test_commands=test or []))
    except TaskError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Approved. Run it: nightshift task run {task_id}")


@task_app.command("reject")
def task_reject(task_id: int, note: Annotated[str | None, typer.Option()] = None) -> None:
    """Gate 1: reject a plan."""
    from nightshift.worker.task import decide

    asyncio.run(decide(task_id, False, note))
    typer.echo("Rejected.")


@task_app.command("revise")
def task_revise(
    task_id: int,
    note: Annotated[str, typer.Option(help="What to change; added to the approved plan.")],
    setup: SetupOpt = None,
    test: Annotated[list[str] | None, typer.Option("--test", help="Replace the plan's test commands.")] = None,
    latest: Annotated[bool, typer.Option(help="Redo it on the default branch's latest commit.")] = False,
    restart: Annotated[
        bool, typer.Option(help="Start from the plan instead of where the last attempt stopped.")
    ] = False,
) -> None:
    """Send a ready or stopped task back to the Coder with a correction, then run it again. By default the run
    picks up where the last attempt stopped: its change, its baseline, and only what failed."""
    from nightshift.worker.task import TaskError, revise

    try:
        asyncio.run(revise(task_id, note, setup, test, latest, "restart" if restart else "continue"))
    except TaskError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Revised. Run it: nightshift task run {task_id}")


@task_app.command("run")
def task_run(task_id: int) -> None:
    """Run an approved task: setup, baseline, Coder, verification, Reviewer. Never touches GitHub."""
    from nightshift.worker.task import TaskError, load_task, run_task, task_dir

    async def run() -> None:
        try:
            task = await run_task(task_id)
        except TaskError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        _, repo = await load_task(task_id)
        typer.echo(_task_line(task))
        if task.error:
            typer.echo(f"Stopped: {task.error}")
        for r in task.result.get("reviews", []):
            typer.echo("\n" + r["markdown"])
        typer.echo(f"\nDiff, verification and review: {task_dir(repo.full_name, task.id)}")

    _run_sandboxed(run())


@task_app.command("draft")
def task_draft(task_id: int) -> None:
    """(Re)write the PR for a ready task. Its testing section comes from the real verification."""
    from nightshift.worker.task import draft_pr, load_task

    async def run() -> None:
        task, repo = await load_task(task_id)
        pr = await draft_pr(task, repo)
        typer.echo(f"# {pr['title']}\n\n{pr['body']}\n---\nCommit:\n{pr['commit_message']}")

    _run_sandboxed(run())


@task_app.command("claim")
def task_claim(
    task_id: int, yes: Annotated[bool, typer.Option("--yes", help="Post it. Without this, only show it.")] = False
) -> None:
    """Post the approved plan's claim comment on the issue (checks the issue is still free first)."""
    from nightshift.worker.task import claim

    typer.echo(asyncio.run(claim(task_id, confirm=yes)))
    if not yes:
        typer.echo(f"\nTo post it: nightshift task claim {task_id} --yes")


@task_app.command("publish")
def task_publish(
    task_id: int,
    yes: Annotated[bool, typer.Option("--yes", help="Gate 2: push and open the PR. Without this, a dry run.")] = False,
) -> None:
    """Gate 2. Checks freshness, applies the fix to the current upstream branch, then (with --yes) opens the PR."""
    from nightshift.worker.task import gate2

    async def run() -> None:
        info = await gate2(task_id, confirm=yes)
        typer.echo(f"Branch {info['branch']} on {info['base']} @ {info['head'][:7]}; files: {', '.join(info['files'])}")
        for b in info["blockers"]:
            typer.echo(f"BLOCKED: {b}")
        if info["drift"]:
            typer.echo("BLOCKED: upstream changed " + ", ".join(info["drift"]) + " since the fix was verified")
        typer.echo(f"\n# {info['title']}\n\n{info['body']}")
        if info["published"]:
            typer.echo(f"\nOpened {info['url']}")
        elif not (info["blockers"] or info["drift"]):
            typer.echo(f"\nDry run. To open this PR: nightshift task publish {task_id} --yes")

    _run_sandboxed(run())


# ---------- operations ----------


async def _set_control(**values: bool) -> None:
    async with session_scope() as s:
        control = await s.get(Control, 1)
        if control is None:
            control = Control(id=1)
            s.add(control)
        for k, v in values.items():
            setattr(control, k, v)


@app.command()
def kill() -> None:
    """Block every model call immediately, fleet-wide."""
    asyncio.run(_set_control(killed=True))
    typer.echo("Kill switch ON. No model calls will run until `nightshift resume`.")


@app.command()
def resume() -> None:
    """Clear the kill switch and pause."""
    asyncio.run(_set_control(killed=False, paused=False))
    typer.echo("Resumed.")


@app.command()
def spend() -> None:
    """Show model spend."""
    s = asyncio.run(spend_summary())
    typer.echo(f"Today: ${s['today']:.2f}   Last 7 days: ${s['last_7_days']:.2f}   This month: ${s['this_month']:.2f}")


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    host: Annotated[str, typer.Option(help="Interface. Keep 127.0.0.1: the app has no login.")] = "127.0.0.1",
    reload: Annotated[
        bool, typer.Option(help="Restart when NightShift's code changes (development). Restarts stop running jobs.")
    ] = False,
) -> None:
    """Start the web app: watchlist, live jobs, reports and issue chat."""
    import uvicorn

    typer.echo(f"NightShift UI on http://{host}:{port}" + (" (reloads on code changes)" if reload else ""))
    src = str(Path(__file__).resolve().parent)
    uvicorn.run(
        "nightshift.web.app:app", host=host, port=port, log_level="warning", reload=reload,
        reload_dirs=[src] if reload else None,
    )  # fmt: skip


@app.command("backup")
def backup_cmd() -> None:
    """Dump the database to backups/ (restore with `pg_restore -d <url> <file>`)."""
    try:
        typer.echo(f"Saved {backup('manual')}")
    except BackupError as e:
        typer.echo(f"Backup failed: {e}", err=True)
        raise typer.Exit(1) from e


@app.command("db-setup")
def db_setup() -> None:
    """Create LangGraph checkpoint tables. Run `alembic upgrade head` first for the app tables."""
    from nightshift.health.graph import setup_checkpointer

    asyncio.run(setup_checkpointer())
    typer.echo("Checkpoint tables ready.")
