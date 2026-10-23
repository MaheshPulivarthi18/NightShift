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

import anthropic
import structlog
import typer
from dotenv import load_dotenv
from sqlalchemy import select

from nightshift import events
from nightshift.config import get_settings
from nightshift.db import Control, Feedback, Repo, Report, session_scope
from nightshift.github import GitHubError
from nightshift.llm import BudgetExceeded, KillSwitchActive, spend_summary
from nightshift.memory import get_or_create_repo

app = typer.Typer(help="NightShift: autonomous repo maintainer agents.", no_args_is_help=True)
watch_app = typer.Typer(help="Manage the watchlist of public repos.", no_args_is_help=True)
report_app = typer.Typer(help="Repo Health Reports.", no_args_is_help=True)
sandbox_app = typer.Typer(help="Docker sandboxes where repo code runs.", no_args_is_help=True)
context_app = typer.Typer(
    help="Repo context for agents: architecture map (L1) and change context (L3).", no_args_is_help=True
)
app.add_typer(watch_app, name="watch")
app.add_typer(report_app, name="report")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(context_app, name="context")

log = structlog.get_logger()

WATCHLIST_CAP = 10
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
            count = len((await s.execute(select(Repo).where(Repo.connected.is_(False)))).scalars().all())
            for raw in repos:
                name = _repo_name(raw)
                exists = (await s.execute(select(Repo).where(Repo.full_name == name))).scalar_one_or_none()
                if exists:
                    typer.echo(f"Already watching {name}")
                    continue
                if count >= WATCHLIST_CAP:
                    typer.echo(f"Watchlist is full ({WATCHLIST_CAP}). Skipping {name}.")
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
            typer.echo(
                f"{r.full_name:45} {kind:10} {r.role:12} last fetched: {fetched}{'  (paused)' if r.paused else ''}"
            )

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
def watch_remove(repo: str) -> None:
    """Remove a repo and everything stored about it."""

    async def run() -> None:
        name = _repo_name(repo)
        async with session_scope() as s:
            r = (await s.execute(select(Repo).where(Repo.full_name == name))).scalar_one_or_none()
            if r is None:
                typer.echo(f"Not watching {name}")
                return
            await s.delete(r)
        typer.echo(f"Removed {name}")

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
            rows = (await s.execute(select(Repo).where(Repo.paused.is_(False)).order_by(Repo.full_name))).scalars()
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
            except anthropic.AuthenticationError:
                typer.echo("Claude API key missing or invalid. Check ANTHROPIC_API_KEY / OPENROUTER_API_KEY.", err=True)
                return
            except anthropic.APIStatusError as e:
                if e.status_code != 402:
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
        name = _repo_name(repo)
        box, cached = await prepare_repo(name, ref, setup or [], fetch_url=fetch)
        async with box:
            typer.echo(f"Sandbox ready ({'cached image' if cached else 'fresh setup, image saved'})")
            ok = True
            for command in test or []:
                res = await box.exec(command)
                status = "PASS" if res.ok else ("TIMEOUT" if res.timed_out else f"FAIL (exit {res.code})")
                typer.echo(f"\n$ {command}\n{status} in {res.seconds:.0f}s\n{res.tail(1500)}")
                ok = ok and res.ok
            return ok

    try:
        ok = asyncio.run(run())
    except SandboxError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    raise typer.Exit(0 if ok else 1)


def _run_sandboxed(coro: Coroutine[Any, Any, None]) -> None:
    """Run a sandbox command, turning sandbox and budget failures into a message and exit code 1."""
    from nightshift.sandbox.docker import SandboxError

    try:
        asyncio.run(coro)
    except (SandboxError, KillSwitchActive, BudgetExceeded) as e:
        typer.echo(f"{type(e).__name__}: {e}", err=True)
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
) -> None:
    """Start the web app: watchlist, live jobs, reports and issue chat."""
    import uvicorn

    typer.echo(f"NightShift UI on http://{host}:{port}")
    uvicorn.run("nightshift.web.app:app", host=host, port=port, log_level="warning")


@app.command("db-setup")
def db_setup() -> None:
    """Create LangGraph checkpoint tables. Run `alembic upgrade head` first for the app tables."""
    from nightshift.health.graph import setup_checkpointer

    asyncio.run(setup_checkpointer())
    typer.echo("Checkpoint tables ready.")
