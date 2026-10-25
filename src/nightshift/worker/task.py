"""A contribution task from issue to reviewed fix: plan, Gate 1, then Coder and Reviewer in a sandbox.

    plan_task:  fresh issue -> sandbox -> brief (L0), map (L1), relevant files (L2), environment (Setup
                agent, once per repo) -> Planner -> "planned"
    approve / reject:  Gate 1, the operator's decision
    run_task:   setup -> baseline tests -> Coder -> verification -> Reviewer (-> Coder again, up to the
                review-round limit) -> "ready" with a diff, or "needs_operator" with the work saved

Nothing here writes to GitHub. Limits per task: settings.task_budget_usd (planning included),
task_minutes and coder_max_steps.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select, update

from nightshift import events, outcomes
from nightshift.catalog import ModelConfigError
from nightshift.config import get_settings
from nightshift.context import archmap
from nightshift.context.change import change_context
from nightshift.context.change import to_markdown as change_markdown
from nightshift.db import LLMCall, Repo, RepoContext, Task, session_scope
from nightshift.github import GitHubClient, GitHubError
from nightshift.health.agents import RepoBrief
from nightshift.health.graph import repo_brief
from nightshift.llm import BudgetExceeded, KillSwitchActive, LLMGateway
from nightshift.memory import follow_rename, get_or_create_repo
from nightshift.router import check_choice, family
from nightshift.sandbox.docker import Sandbox, SandboxError, prepare_repo
from nightshift.sandbox.envimage import ensure_env_image
from nightshift.worker import planner, prwriter, publish, reviewer
from nightshift.worker.coder import CoderRun
from nightshift.worker.issue import IssueView, fetch_issue
from nightshift.worker.relevant import relevant_files
from nightshift.worker.setup_agent import ensure_environment
from nightshift.worker.verify import CommandResult, FixCheck, diff, run_commands

log = structlog.get_logger()

RECIPE = "recipe"


class TaskError(RuntimeError):
    pass


def run_blocker(plan: dict[str, Any]) -> str | None:
    """Why this plan cannot be run, or None. A run without a test has nothing to verify the fix with."""
    if plan.get("actionable") is False:
        return (
            "The Planner marked this issue not actionable yet. Ask its questions on the issue, "
            "and plan it again once they are answered."
        )
    if not plan.get("test_commands"):
        what = "the suites could not be checked" if plan.get("kind") == "docs" else "a fix could not be verified"
        return f"The plan has no test command, so {what}. Add one before approving."
    return None


def task_dir(full_name: str, task_id: int) -> Path:
    path = Path(get_settings().reports_dir) / full_name.replace("/", "__") / "tasks" / str(task_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def issue_text(issue: IssueView) -> str:
    parts = [f"#{issue.number} {issue.title} ({issue.state}, by {issue.author}, labels: {', '.join(issue.labels)})"]
    parts.append(issue.body or "(no description)")
    for c in issue.comments:
        parts.append(f"--- comment by {c['author']} at {c['created_at']}\n{c['body']}")
    return "\n\n".join(parts)


def brief_text(brief: RepoBrief | None) -> str:
    if brief is None:
        return "(no repo brief)"
    lines = [brief.summary, "", "PR rules:", *[f"- {r}" for r in brief.pr_rules]]
    lines += ["", "Setup and test (from the docs):", *[f"- {c}" for c in brief.setup_and_test]]
    return "\n".join(lines)


async def _recipe(repo_id: int) -> dict[str, Any] | None:
    async with session_scope() as s:
        row = (
            await s.execute(
                select(RepoContext)
                .where(RepoContext.repo_id == repo_id, RepoContext.kind == RECIPE)
                .order_by(RepoContext.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return row.data if row else None


async def save_recipe(repo_id: int, commit: str, setup: list[str], tests: list[CommandResult]) -> None:
    """Commands that set the repo up and ran its tests at `commit`; offered to the next plan."""
    data = {"setup": setup, "tests": [t.command for t in tests], "passing": [t.command for t in tests if t.ok]}
    async with session_scope() as s:
        existing = (
            await s.execute(
                select(RepoContext).where(
                    RepoContext.repo_id == repo_id, RepoContext.kind == RECIPE, RepoContext.commit == commit
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.data = data
        else:
            s.add(RepoContext(repo_id=repo_id, kind=RECIPE, commit=commit, data=data))


async def task_cost(task: Task) -> float:
    async with session_scope() as s:
        q = select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.run_id == task.run_id)
        return float((await s.execute(q)).scalar_one())


async def load_task(task_id: int) -> tuple[Task, Repo]:
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        if task is None:
            raise TaskError(f"No task {task_id}.")
        repo = await s.get(Repo, task.repo_id)
        assert repo is not None
    return task, repo


async def _save(task: Task, **values: Any) -> None:
    async with session_scope() as s:
        row = await s.get(Task, task.id)
        assert row is not None
        for k, v in values.items():
            setattr(row, k, v)
            setattr(task, k, v)


# ---------- plan and Gate 1 ----------


async def precheck(
    full_name: str, number: int, rehearsal: bool = False
) -> tuple[str, IssueView, list[str], str | None]:
    """The fast checks before planning: the repo's current name, the issue, and whether it is still free.

    Returns (current repo name, issue, blockers, note if the repo moved). Raises TaskError when the issue
    cannot be planned, so the caller can say why at once."""
    gh = GitHubClient()
    try:
        current = await gh.canonical_name(full_name)
    except GitHubError as e:
        raise TaskError(f"Cannot read {full_name}: {e}") from e
    finally:
        await gh.aclose()
    moved = f"{full_name} has moved to {current}; this task uses {current}" if current != full_name else None
    if moved:  # the watchlist follows the move, so reports, tasks and links all use the current name
        async with session_scope() as s:
            await follow_rename(s, full_name, current)
    try:
        issue = await fetch_issue(current, number)
    except GitHubError as e:
        raise TaskError(f"Cannot read {current}#{number}: {e}") from e
    blockers = issue.blockers(get_settings().github_login or None)
    if blockers and not rehearsal:
        raise TaskError(f"#{number} is not free: " + "; ".join(blockers))
    return current, issue, blockers, moved


async def plan_task(
    full_name: str, number: int, *, base: str | None = None, rehearsal: bool = False, note: str = ""
) -> Task:
    """Plan a fix for an issue and stop at Gate 1. A rehearsal may plan an issue someone already fixed.
    `note` is the developer's own instruction to the Planner, e.g. "treat this as a docs task"."""
    settings = get_settings()
    full_name, issue, blockers, moved = await precheck(full_name, number, rehearsal)

    async with session_scope() as s:
        repo = await get_or_create_repo(s, full_name)
        task = Task(
            repo_id=repo.id, issue_number=number, status="planning", base_commit="", rehearsal=rehearsal,
            issue=issue.model_dump(mode="json"), plan={},
        )  # fmt: skip
        s.add(task)
    await events.start_run(task.run_id, repo.id)
    beat = start_heartbeat(task.id)
    llm = LLMGateway(task.run_id, repo.id, run_budget_usd=settings.task_budget_usd)
    box: Sandbox | None = None
    try:
        box, _ = await prepare_repo(full_name, base or "HEAD", [])
        commit = (await box.exec("git rev-parse HEAD")).stdout.strip()
        await events.record(task.run_id, "node_start", "context", node="context")
        brief, _ = await repo_brief(llm, full_name)
        amap = await archmap.architecture_map(box, repo, llm)
        _, l2 = await relevant_files(
            box, f"{issue.title}\n{issue.body}\n" + "\n".join(c["body"] for c in issue.comments)
        )
        recipe = await _recipe(repo.id)
        env = await ensure_environment(llm, repo, box, commit)
        image = await ensure_env_image(env.system_packages, env.tools) if env.status == "ready" else None
        tracked = set((await box.exec("git ls-files")).stdout.splitlines())

        await events.record(task.run_id, "node_start", "plan", node="plan")
        context = (
            f"<untrusted_github_data>\n<issue>\n{issue_text(issue)}\n</issue>\n</untrusted_github_data>\n\n"
            f"<repo_brief>\n{brief_text(brief)}\n</repo_brief>\n\n"
            f"<architecture_map>\n{archmap.to_markdown(amap)}\n</architecture_map>\n\n"
            f"<relevant_files>\n{l2}\n</relevant_files>\n\n"
            f"<sandbox_environment>\n{env.text()}\n</sandbox_environment>\n\n"
            f"<commands_that_worked_before>\n{recipe or 'none yet'}\n</commands_that_worked_before>"
        )
        if note.strip():  # written by the developer in NightShift, not on GitHub: trusted, and it sets the scope
            context += f"\n\n<developer_note>\n{note.strip()}\n</developer_note>"
        plan = await planner.make_plan(llm, repo=full_name, context=context)

        # Run the setup now, before Gate 1, so a plan whose commands cannot work is caught before any paid run.
        # A failure goes back to the Planner once. Success also saves the repo image the run will start from.
        await events.record(task.run_id, "node_start", "setup check", node="setup_check")
        setup_error = await check_setup(full_name, commit, plan.setup_commands, image)
        if setup_error:
            await events.record(task.run_id, "warning", "setup failed; asking the Planner to fix the commands")
            failed = "\n".join(plan.setup_commands)
            plan = await planner.make_plan(
                llm,
                repo=full_name,
                context=f"{context}\n\n<setup_failed>\nThese setup commands failed in the sandbox:\n{failed}\n\n"
                f"Output:\n{setup_error}\n</setup_failed>",
            )
            setup_error = await check_setup(full_name, commit, plan.setup_commands, image)
        checks = ["setup commands ran successfully in the sandbox"] if not setup_error else []
        warnings = [f"setup failed in the sandbox: {setup_error[-600:]}"] if setup_error else []
        if env.status == "ready":
            passing, total = sum(t.ok for t in env.tests), len(env.tests)
            checks.append(f"sandbox environment ready: {passing} of {total} test commands pass on {env.commit[:7]}")
        else:
            why = ": ".join(filter(None, [env.blocker, env.detail]))
            warnings.append(f"Setup agent: environment {env.status}: {why}")
        warnings += [f"`{p}` does not exist in the repo" for p in planner.unknown_paths(plan, tracked)]
        warnings += [f"rehearsal: {b}" for b in blockers]
        warnings += [moved] if moved else []
        markdown = planner.to_markdown(
            plan, title=f"{full_name}#{number} {issue.title}", warnings=warnings, checks=checks, note=note.strip()
        )
        (task_dir(full_name, task.id) / "plan.md").write_text(markdown)
        await _save(
            task,
            status="planned",
            base_commit=commit,
            plan={
                **plan.model_dump(mode="json"),
                "warnings": warnings,
                "markdown": markdown,
                "setup_verified": not setup_error,
                "planning_note": note.strip(),
                "environment": {"system_packages": env.system_packages, "tools": env.tools}
                if env.status == "ready"
                else {},
            },  # fmt: skip
            result={"brief": brief_text(brief), "map_commit": amap["meta"]["commit"]},
            cost_usd=await task_cost(task),
        )
        await events.finish_run(task.run_id, "done")
        return task
    except BaseException as e:
        await _save(task, status="failed", error=f"{type(e).__name__}: {e}", cost_usd=await task_cost(task))
        await events.finish_run(task.run_id, "failed", error=str(e))
        raise
    finally:
        beat.cancel()
        if box:
            await box.remove()


async def check_setup(full_name: str, commit: str, setup: list[str], image: str | None = None) -> str | None:
    """Run the setup commands in a fresh sandbox. Returns None if they work, else the end of their output."""
    try:
        box, _ = await prepare_repo(full_name, commit, setup, image=image)
    except SandboxError as e:
        return str(e)[-3000:]
    await box.remove()
    return None


async def decide(
    task_id: int,
    approve: bool,
    note: str | None = None,
    models: dict[str, str] | None = None,
    **overrides: list[str],
) -> Task:
    """Gate 1. `overrides` may replace the plan's setup_commands or test_commands; `models` picks the
    model to try first for the Coder, the Reviewer or the PR writer (router.CHOOSABLE)."""
    task, _ = await load_task(task_id)
    if task.status != "planned":
        raise TaskError(f"Task {task_id} is {task.status}, not waiting for approval.")
    plan = dict(task.plan)
    plan.update({k: v for k, v in overrides.items() if v})
    if approve and (why := run_blocker(plan)):
        raise TaskError(why)
    chosen = {agent: model.strip() for agent, model in (models or {}).items() if model and model.strip()}
    for agent, model in chosen.items():
        try:
            await check_choice(agent, model)
        except ModelConfigError as e:
            raise TaskError(str(e)) from e
    plan["models"] = chosen
    if approve:
        await _save(task, status="approved", plan=plan, note=note, approved_at=datetime.now(UTC))
    else:
        await _save(task, status="rejected", note=note)
    await _planner_outcome(task, approve)
    return task


async def latest_commit(full_name: str) -> str:
    """The head commit of the repo's default branch, as GitHub reports it now."""
    gh = GitHubClient()
    try:
        branch = (await gh.rest_get(f"/repos/{full_name}"))["default_branch"]
        return str((await gh.rest_get(f"/repos/{full_name}/commits/{branch}"))["sha"])
    finally:
        await gh.aclose()


async def revise(
    task_id: int,
    note: str,
    setup_commands: list[str] | None = None,
    test_commands: list[str] | None = None,
    latest: bool = False,
) -> Task:
    """Send a finished or stopped task back for another run, with the operator's correction added to
    the approved plan (and, if given, new setup or test commands). The run starts again from the same
    base commit; the old result is replaced."""
    await mark_stale_runs()
    task, _ = await load_task(task_id)
    if task.status not in ("ready", "needs_operator", "failed", "published"):
        raise TaskError(f"Task {task_id} is {task.status}; it cannot be revised now.")
    combined = f"{task.note}\n\nRevision: {note}" if task.note else f"Revision: {note}"
    # A published task keeps its PR and claim, so review feedback becomes a follow-up commit on that PR.
    keep = {k: v for k, v in task.result.items() if k in ("brief", "map_commit", "pr_url", "claim_url")}
    plan = dict(task.plan)
    if setup_commands:
        plan["setup_commands"] = setup_commands
    if test_commands:
        plan["test_commands"] = test_commands
    if why := run_blocker(plan):
        raise TaskError(why)
    if latest:  # upstream moved on (Gate 2 drift): redo the fix on the current default branch
        _, repo = await load_task(task_id)
        base = await latest_commit(repo.full_name)
        combined += f"\n\nRebased from {task.base_commit[:7]} to {base[:7]} (latest default branch)."
        await _save(task, base_commit=base)
    await _save(task, status="approved", note=combined, plan=plan, result=keep, diff=None, error=None)
    return task


# ---------- work ----------


CODE_PATH = re.compile(r"[\w./-]+\.(?:py|pyi|ts|tsx|js|jsx|go|rs|java|kt|c|h|cc|cpp|hpp|cs|rb|php|swift)\b")
DOCS_SOURCE_CHARS = 40_000


async def _docs_sources(box: Sandbox, plan_markdown: str) -> str:
    """For reviewing a docs change: the code files the plan names, so each claim can be checked."""
    tracked = set((await box.exec("git ls-files")).stdout.splitlines())
    paths = [p for p in dict.fromkeys(CODE_PATH.findall(plan_markdown)) if p in tracked]
    if not paths:  # the plan names a file by its base name only, e.g. clangd_language_server.py
        names = {p.rsplit("/", 1)[-1] for p in CODE_PATH.findall(plan_markdown)}
        paths = [p for p in sorted(tracked) if p.rsplit("/", 1)[-1] in names]
    parts, total = [], 0
    for path in paths[:8]:
        text = (await box.exec(f"head -c 15000 -- {shlex.quote(path)}")).stdout
        if total + len(text) > DOCS_SOURCE_CHARS:
            break
        parts.append(f'<file path="{path}">\n{text}\n</file>')
        total += len(text)
    return "\n".join(parts)


async def _coder_outcome(task: Task, coder: CoderRun) -> None:
    """What the router learns from a Coder round: did this model's fix pass verification?"""
    passed = bool(coder.check and coder.check.passed)
    why = "fix verified" if passed else (coder.stopped or "verification did not pass")
    await outcomes.record("coder", coder.model, ok=passed, kind="task", run_id=task.run_id, detail=why)


async def _planner_outcome(task: Task, approved: bool) -> None:
    """Gate 1 teaches the router about the Planner: an approved plan is a success, a rejected one a
    failure. A plan that asked questions instead (not actionable) is not held against the model."""
    if not approved and task.plan.get("actionable") is False:
        return
    async with session_scope() as s:
        model = (
            await s.execute(
                select(LLMCall.model)
                .where(LLMCall.run_id == task.run_id, LLMCall.agent == "planner", LLMCall.error.is_(None))
                .order_by(LLMCall.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    detail = "plan approved" if approved else "plan rejected"
    await outcomes.record("planner", model or "", ok=approved, kind="task", run_id=task.run_id, detail=detail)


# ---------- runs that died ----------

HEARTBEAT_SECONDS = 30
STALE_AFTER = timedelta(minutes=3)
STALE_ERROR = "the run stopped without finishing (its process ended). Revise to run it again."


def start_heartbeat(task_id: int) -> asyncio.Task[None]:
    """Record every half minute that this task's run is alive. The caller cancels it when the run ends."""

    async def beat() -> None:
        while True:
            try:
                async with session_scope() as s:
                    await s.execute(
                        update(Task)
                        .where(Task.id == task_id)
                        .values(heartbeat_at=func.now(), updated_at=Task.updated_at)  # a beat is not an update
                    )
            except Exception as e:  # pragma: no cover - database trouble; the run itself goes on
                log.warning("heartbeat_failed", task=task_id, error=str(e))
            await asyncio.sleep(HEARTBEAT_SECONDS)

    return asyncio.create_task(beat())


async def mark_stale_runs() -> list[int]:
    """Tasks still "planning" or "working" whose run stopped beating (the process was killed or closed):
    a stale plan becomes "failed", a stale run "needs_operator", so they can be revised or planned again."""
    cutoff = datetime.now(UTC) - STALE_AFTER
    async with session_scope() as s:
        stale = list(
            (
                await s.execute(
                    select(Task).where(
                        Task.status.in_(("planning", "working")),
                        func.coalesce(Task.heartbeat_at, Task.updated_at) < cutoff,
                    )
                )
            ).scalars()
        )
        for t in stale:
            t.status = "failed" if t.status == "planning" else "needs_operator"
            t.error = STALE_ERROR
    for t in stale:
        await events.finish_run(t.run_id, "failed", error=STALE_ERROR)
        log.warning("stale_run", task=t.id, status=t.status)
    return [t.id for t in stale]


async def archive(task_id: int, archived: bool) -> Task:
    """Take a task off the board (kept, with its history), or put it back."""
    task, _ = await load_task(task_id)
    if archived and task.status in ("planning", "working"):
        raise TaskError(f"Task {task_id} is {task.status}; archive it once it stops.")
    await _save(task, archived_at=datetime.now(UTC) if archived else None)
    return task


async def run_task(task_id: int) -> Task:
    """Set up, record a baseline, then Coder and Reviewer rounds until the fix is verified and approved."""
    settings = get_settings()
    task, repo = await load_task(task_id)
    if task.status != "approved":
        raise TaskError(f"Task {task_id} is {task.status}; only an approved plan can run.")
    if why := run_blocker(task.plan):
        raise TaskError(why)
    plan = dict(task.plan)
    if task.note:  # the operator's word at Gate 1 is part of what was approved, for the Coder and the Reviewer
        plan["markdown"] += (
            f"\n\n## Developer's note at approval (overrides the plan where they conflict)\n\n{task.note}"
        )
    issue = IssueView.model_validate(task.issue)
    folder = task_dir(repo.full_name, task.id)
    deadline = time.monotonic() + settings.task_minutes * 60
    llm = LLMGateway(task.run_id, repo.id, run_budget_usd=settings.task_budget_usd)
    result: dict[str, Any] = dict(task.result)
    await _save(task, status="working", error=None)
    await events.start_run(task.run_id, repo.id)
    beat = start_heartbeat(task.id)
    box: Sandbox | None = None
    try:
        await events.record(task.run_id, "node_start", "setup", node="setup")
        env = plan.get("environment") or {}
        image = await ensure_env_image(env.get("system_packages", []), env.get("tools", []))
        box, cached = await prepare_repo(repo.full_name, task.base_commit, plan["setup_commands"], image=image)
        await events.record(task.run_id, "node_start", "baseline", node="baseline")
        baseline = await run_commands(box, plan["test_commands"])
        result["baseline"] = [b.model_dump() for b in baseline]
        await save_recipe(repo.id, task.base_commit, plan["setup_commands"], baseline)

        amap = await archmap.load_map(repo.id, result.get("map_commit", task.base_commit))
        context = (
            f"<untrusted_github_data>\n<issue>\n{issue_text(issue)}\n</issue>\n</untrusted_github_data>\n\n"
            f"<approved_plan>\n{plan['markdown']}\n</approved_plan>\n\n"
            f"<repo_brief>\n{result.get('brief', '')}\n</repo_brief>\n\n"
            + (f"<architecture_map>\n{archmap.to_markdown(amap)}\n</architecture_map>\n\n" if amap else "")
            + "<baseline_tests>\n"
            + "\n".join(b.line() for b in baseline)
            + "\n</baseline_tests>"
        )
        chosen = plan.get("models") or {}  # the operator's picks at Gate 1
        coder = CoderRun(box, llm, task.base_commit, plan["test_commands"], baseline, context)
        coder.prefer = chosen.get("coder")
        coder.kind = plan.get("kind", "code")
        # A large task starts one step up the ladder, unless the operator chose the model.
        coder.level = 1 if plan.get("size") == "large" and not coder.prefer else 0
        escalated = False
        if plan.get("kind") == "docs":
            instruction = (
                "This is a docs change: write the documentation the approved plan describes, editing only "
                "documentation files. Read the code each statement and example describes first, and run an "
                "example when you can: nothing may claim behaviour the code does not have. Then call submit "
                "with an empty targeted test command; code checks that only documentation files changed and "
                "that the suites still pass."
            )
        else:
            instruction = (
                "Fix the issue following the approved plan. Start by reading the files it names. "
                f"The plan's targeted test command is `{plan['test_plan']['targeted_command']}`."
            )
        reviews: list[dict[str, Any]] = []
        status, why = "needs_operator", ""
        for round_no in range(1, settings.review_rounds + 2):
            await events.record(task.run_id, "node_start", f"coder round {round_no}", node="coder")
            await coder.work(instruction, deadline)
            await _coder_outcome(task, coder)
            failed = coder.check is None or not coder.check.passed
            if failed and not escalated and time.monotonic() < deadline and coder.escalate():
                escalated = True  # once per task: a stronger model takes over the same conversation
                stronger = coder.ladder[coder.level]
                await events.record(task.run_id, "node_start", f"coder escalated to {stronger}", node="coder")
                await coder.work(
                    "A stronger model takes over this task, with the conversation so far. The previous attempt "
                    f"stopped because: {coder.stopped or 'verification did not pass'}. Check the work so far, "
                    "then finish the fix and call submit.",
                    deadline,
                )
                await _coder_outcome(task, coder)
            if coder.check is None or not coder.check.passed:
                why = coder.stopped or "verification did not pass"
                break
            await events.record(task.run_id, "node_start", f"review round {round_no}", node="review")
            pack, _ = change_markdown(await change_context(box, task.base_commit), budget_tokens=6000)
            rv = await reviewer.review(
                llm,
                repo=repo.full_name,
                issue=issue_text(issue),
                plan=plan["markdown"],
                diff=await diff(box, task.base_commit),
                verification=coder.check.report(),
                rules=result.get("brief", ""),
                change_context=pack,
                avoid=family(coder.model),
                prefer=chosen.get("reviewer"),
                sources=await _docs_sources(box, plan["markdown"]) if coder.kind == "docs" else "",
            )
            reviews.append(
                {"round": round_no, **rv.model_dump(mode="json"), "markdown": reviewer.to_markdown(rv, round_no)}
            )
            if rv.verdict == "approve" and not reviewer.blocking(rv) and not any(c.needs_change for c in rv.related):
                status = "ready"
                break
            instruction = reviewer.feedback_for_coder(rv)
            why = "the review still asks for changes after the last round"

        final_diff = await diff(box, task.base_commit)
        result.update(
            reviews=reviews,
            check=coder.check.model_dump() if coder.check else None,
            summary=coder.summary,
            coder_steps=coder.steps,
            from_cached_image=cached,
        )
        (folder / "diff.patch").write_text(final_diff)
        (folder / "verification.md").write_text(coder.check.report() if coder.check else "No verification ran.")
        (folder / "review.md").write_text("\n\n".join(r["markdown"] for r in reviews) or "No review ran.")
        await _save(
            task, status=status, diff=final_diff, result=result,
            error=None if status == "ready" else why, cost_usd=await task_cost(task),
        )  # fmt: skip
        if status == "ready":
            await events.record(task.run_id, "node_start", "pr draft", node="pr_writer")
            await draft_pr(task, repo, llm)
        await events.finish_run(task.run_id, "done" if status == "ready" else "stopped", error=task.error)
        return task
    except (BudgetExceeded, KillSwitchActive, SandboxError) as e:
        if box:
            result["partial_diff"] = await diff(box, task.base_commit)
        await _save(task, status="needs_operator", result=result, error=f"{type(e).__name__}: {e}",
                    cost_usd=await task_cost(task))  # fmt: skip
        await events.finish_run(task.run_id, "stopped", error=str(e))
        return task
    except BaseException as e:
        if box:  # keep the Coder's work, whatever stopped the run
            try:
                result["partial_diff"] = await diff(box, task.base_commit)
                (folder / "partial.patch").write_text(result["partial_diff"])
            except Exception:
                pass
        await _save(task, status="failed", result=result, error=f"{type(e).__name__}: {e}",
                    cost_usd=await task_cost(task))  # fmt: skip
        await events.finish_run(task.run_id, "failed", error=str(e))
        raise
    finally:
        beat.cancel()
        if box:
            await box.remove()


# ---------- PR draft, claim and Gate 2 ----------


async def _repo_conventions(full_name: str) -> tuple[str, list[str]]:
    """The repo's PR template and the titles of its recently merged PRs."""
    gh = GitHubClient()
    try:
        docs = (await gh.fetch_repo_docs(full_name))["docs"]
        pulls = await gh.rest_get(f"/repos/{full_name}/pulls?state=closed&per_page=30")
    finally:
        await gh.aclose()
    template = next((t for p, t in docs.items() if "pull_request_template" in p.lower()), "")
    return template, [p["title"] for p in pulls if p.get("merged_at")][:15]


async def draft_pr(task: Task, repo: Repo, llm: LLMGateway | None = None) -> dict[str, Any]:
    """Write the PR (title, body with code-built evidence, commit message) for a ready task."""
    if task.status not in ("ready", "published") or not task.result.get("check"):
        raise TaskError(f"Task {task.id} has no verified fix to write a PR for.")
    llm = llm or LLMGateway(task.run_id, repo.id, run_budget_usd=get_settings().task_budget_usd)
    check = FixCheck.model_validate(task.result["check"])
    template, titles = await _repo_conventions(repo.full_name)
    reviews = task.result.get("reviews", [])
    draft = await prwriter.draft_pr(
        llm,
        repo=repo.full_name,
        issue=issue_text(IssueView.model_validate(task.issue)),
        diff=task.diff or "",
        review=reviews[-1]["markdown"] if reviews else "",
        rules=task.result.get("brief", ""),
        template=template,
        recent_titles=titles,
        prefer=(task.plan.get("models") or {}).get("pr_writer"),
    )
    pr = {
        "title": draft.title,
        "body": prwriter.pr_body(draft, check, task.issue_number),
        "commit_message": prwriter.commit_message(draft),
    }
    (task_dir(repo.full_name, task.id) / "pr.md").write_text(f"# {pr['title']}\n\n{pr['body']}")
    await _save(task, result={**task.result, "pr": pr}, cost_usd=await task_cost(task))
    return pr


async def claim(task_id: int, *, confirm: bool) -> str:
    """Post the plan's claim comment. Without `confirm`, only says what would be posted. For a plan
    that is not actionable, the comment asks the Planner's questions, so it can be posted at Gate 1."""
    task, repo = await load_task(task_id)
    asking = task.status == "planned" and task.plan.get("actionable") is False
    if task.status in ("planned", "rejected", "failed") and not asking:
        raise TaskError(f"Task {task_id} is {task.status}; claim after approving the plan.")
    text = task.plan.get("claim_comment", "")
    if not confirm:
        return f"Would post on {repo.full_name}#{task.issue_number}:\n\n{text}"
    url = await publish.post_claim(repo, task, text)
    await _save(task, result={**task.result, "claim_url": url})
    return url


async def gate2(task_id: int, *, confirm: bool) -> dict[str, Any]:
    """Prepare the PR on the current upstream branch; with `confirm`, push and open it."""
    task, repo = await load_task(task_id)
    if task.status != "ready":
        raise TaskError(f"Task {task_id} is {task.status}; only a ready task can be published.")
    pr = task.result.get("pr") or await draft_pr(task, repo)
    blockers = await publish.freshness(repo.full_name, task)
    prepared = await publish.prepare(repo, task, task_dir(repo.full_name, task.id) / "publish")
    info = publish.summary(prepared, pr["title"], pr["body"]) | {"blockers": blockers}
    if not confirm or blockers or prepared.drift:
        return info | {"published": False}
    url = await publish.publish(repo, task, prepared, title=pr["title"], body=pr["body"], message=pr["commit_message"])
    await _save(task, status="published", result={**task.result, "pr_url": url})
    return info | {"published": True, "url": url}
