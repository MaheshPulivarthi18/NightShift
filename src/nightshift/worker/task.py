"""A contribution task from issue to reviewed fix: plan, Gate 1, then Coder and Reviewer in a sandbox.

    plan_task:  fresh issue -> sandbox -> brief (L0), map (L1), relevant files (L2), environment (Setup
                agent, once per repo) -> Planner -> plan check -> "planned"
    approve / reject:  Gate 1, the operator's decision
    run_task:   setup -> baseline tests -> Coder (submit: scope, then fail before / pass after) -> Reviewer
                -> full suites, once approved (a regression or a review's changes -> Coder again, up to the
                review-round limit) -> "ready" with a diff, or "needs_operator" with the work saved

Checks run cheapest first, so a wrong change is caught before the slow ones run (verify.py); each one is
kept on result["checks"], the task's Checks.

Nothing here writes to GitHub. Limits per task: settings.task_budget_usd (planning included),
task_minutes and coder_max_steps.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult

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
from nightshift.llm import BudgetExceeded, KillSwitchActive, LLMGateway, ModelAPIError
from nightshift.memory import follow_rename, get_or_create_repo
from nightshift.router import check_choice, family
from nightshift.sandbox.docker import Sandbox, SandboxError, prepare_repo
from nightshift.sandbox.envimage import ensure_env_image
from nightshift.worker import feedback, plancheck, planner, prwriter, publish, reviewer
from nightshift.worker.coder import CoderRun
from nightshift.worker.issue import IssueView, fetch_issue
from nightshift.worker.relevant import coverage, relevant_files
from nightshift.worker.setup_agent import RECIPE, ensure_environment, environment_for, load_recipe
from nightshift.worker.verify import (
    CommandResult,
    FixCheck,
    Scope,
    check_suites,
    diff,
    run_commands,
    run_reproduction,
    step,
)

log = structlog.get_logger()


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


async def precheck(full_name: str, number: int) -> tuple[str, IssueView, list[str], str | None]:
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
    if blockers:
        raise TaskError(f"#{number} is not free: " + "; ".join(blockers))
    return current, issue, blockers, moved


async def plan_task(full_name: str, number: int, *, base: str | None = None, note: str = "") -> Task:
    """Plan a fix for an issue and stop at Gate 1, if the issue is still free.
    `note` is the developer's own instruction to the Planner, e.g. "treat this as a docs task"."""
    settings = get_settings()
    full_name, issue, _, moved = await precheck(full_name, number)

    async with session_scope() as s:
        repo = await get_or_create_repo(s, full_name)
        task = Task(
            repo_id=repo.id, issue_number=number, status="planning", base_commit="",
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
        l2_paths, l2 = await relevant_files(
            box, f"{issue.title}\n{issue.body}\n" + "\n".join(c["body"] for c in issue.comments)
        )
        recipe = await load_recipe(repo.id)
        env = await ensure_environment(llm, repo, box, commit)
        image = await ensure_env_image(env.system_packages, env.tools) if env.status == "ready" else None
        tracked = set((await box.exec("git ls-files")).stdout.splitlines())

        await events.record(task.run_id, "node_start", "plan", node="plan")
        inputs = {
            "repo": full_name, "brief": brief_text(brief), "architecture_map": archmap.to_markdown(amap),
            "environment": env.text(), "recipe": json.dumps(recipe, indent=1, sort_keys=True) if recipe else "",
            "issue": issue_text(issue), "relevant_files": l2, "note": note.strip(),
        }  # fmt: skip
        plan = await planner.make_plan(llm, planner.plan_prompt(**inputs))

        # An independent check of the plan against the code; must-fix findings send it back to the Planner once.
        # A plan with nothing to code (it asks the maintainers first) has nothing to check.
        pcheck = plancheck.PlanCheck(summary="Not checked: the plan asks questions before any code.", findings=[])
        rechecked: list[plancheck.Addressed] = []
        open_findings: list[str] = []  # must-fix findings the revised plan still misses, for Gate 1
        if plan.actionable:
            await events.record(task.run_id, "node_start", "plan check", node="plan_check")
            draft = planner.to_markdown(plan, title=f"{full_name}#{number} {issue.title}", warnings=[])
            pcheck = await plancheck.check_plan(
                llm, box, issue=issue_text(issue), plan=draft, avoid=family(llm.last_model)
            )
        revised = bool(pcheck.must())
        if revised:
            musts = pcheck.must()
            what = ", ".join(sorted({f.kind.replace("_", " ") for f in musts}))
            await events.record(
                task.run_id, events.CAUGHT,
                f"Plan check caught {len(musts)} must-fix problem{'s' if len(musts) > 1 else ''} ({what}): "
                "sent back to the Planner",
            )  # fmt: skip
            inputs["plan_check"] = plancheck.as_note(musts)
            plan = await planner.make_plan(llm, planner.plan_prompt(**inputs))
            revised_draft = planner.to_markdown(plan, title=f"{full_name}#{number} {issue.title}", warnings=[])
            rechecked = await plancheck.recheck(llm, musts, revised_draft, avoid=family(llm.last_model))
            still = [f for f, r in zip(musts, rechecked, strict=True) if not r.addressed]
            if still:  # warned at Gate 1, not re-planned again: one revision is the cap
                await events.record(
                    task.run_id, "warning",
                    f"The revised plan still does not address {len(still)} of {len(musts)} must-fix findings",
                )  # fmt: skip
                open_findings = [
                    f"Plan check: the revised plan does not address `{f.location}`: {f.problem} ({r.how})"
                    for f, r in zip(musts, rechecked, strict=True) if not r.addressed
                ]  # fmt: skip
            else:
                await events.record(
                    task.run_id,
                    events.FIXED,
                    f"Revised plan re-checked: it addresses all {len(musts)} must-fix findings",
                )

        # Run the setup now, before Gate 1, so a plan whose commands cannot work is caught before any paid run.
        # A failure goes back to the Planner once. Success also saves the repo image the run will start from.
        await events.record(task.run_id, "node_start", "setup check", node="setup_check")
        setup_error, repro = await check_setup(full_name, commit, plan.setup_commands, image, plan.reproduction)
        if setup_error:
            await events.record(
                task.run_id, events.CAUGHT, "Setup commands failed in the sandbox: sent back to the Planner"
            )
            failed = "\n".join(plan.setup_commands)
            again = planner.plan_prompt(**inputs, setup_failed=f"{failed}\n\nOutput:\n{setup_error}")
            plan = await planner.make_plan(llm, again)
            setup_error, repro = await check_setup(full_name, commit, plan.setup_commands, image, plan.reproduction)
            if setup_error:
                await events.record(task.run_id, "warning", "The revised setup commands still fail: the plan shows why")
            else:
                await events.record(task.run_id, events.FIXED, "The revised setup commands work in the sandbox")
        checks = ["setup commands ran successfully in the sandbox"] if not setup_error else []
        warnings = [f"setup failed in the sandbox: {setup_error[-600:]}"] if setup_error else []
        warnings += open_findings
        repro_checks, repro_warnings = reproduction_note(repro)
        checks += repro_checks
        warnings += repro_warnings
        if env.status == "ready":
            passing, total = sum(t.ok for t in env.tests), len(env.tests)
            checks.append(f"sandbox environment ready: {passing} of {total} test commands pass on {env.commit[:7]}")
        else:
            why = ": ".join(filter(None, [env.blocker, env.detail]))
            warnings.append(f"Setup agent: environment {env.status}: {why}")
        warnings += [f"`{p}` does not exist in the repo" for p in planner.unknown_paths(plan, tracked)]
        warnings += [moved] if moved else []
        # PRs that share the issue's key terms: not proof someone has it, so a warning to check, not a refusal
        warnings += [f"{w}. Check it before approving" for w in issue.possible(settings.github_login or None)]
        markdown = planner.to_markdown(
            plan, title=f"{full_name}#{number} {issue.title}", warnings=warnings, checks=checks, note=note.strip(),
            plan_check=plancheck.to_markdown(pcheck, revised, rechecked),
        )  # fmt: skip
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
                # The reproduction's run on the code before any change; when it failed, the fix must make it pass.
                "reproduced": repro.model_dump() if repro else None,
                "planning_note": note.strip(),
                "relevant_files": l2_paths,  # for the context check after the run
                "plan_check": {
                    **pcheck.model_dump(mode="json"),
                    "revised": revised,
                    "recheck": [r.model_dump() for r in rechecked],
                },  # fmt: skip
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


async def check_setup(
    full_name: str,
    commit: str,
    setup: list[str],
    image: str | None = None,
    reproduction: planner.Reproduction | None = None,
) -> tuple[str | None, CommandResult | None]:
    """Run the setup commands in a fresh sandbox, then the plan's reproduction there.
    Returns (the end of the setup's output if it failed, else None; the reproduction's run, if any)."""
    try:
        box, _ = await prepare_repo(full_name, commit, setup, image=image)
    except SandboxError as e:
        return str(e)[-3000:], None
    async with box:
        return None, await run_reproduction(box, reproduction.model_dump()) if reproduction else None


def reproduction_note(run: CommandResult | None) -> tuple[list[str], list[str]]:
    """(checks, warnings) for Gate 1 from the reproduction's run on the code before any change."""
    if run is None:
        return [], []
    if run.ok:
        return [], [
            f"the reproduction PASSED on the current code (`{run.command}`), so it does not show the bug: "
            "the plan may misread the issue"
        ]
    tail = run.tail.strip().splitlines()[-1:] or [""]
    return [f"the reproduction fails on the current code, as the issue says ({run.line()}: {tail[0][:200]})"], []


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


Mode = Literal["continue", "restart"]


async def revise(
    task_id: int,
    note: str,
    setup_commands: list[str] | None = None,
    test_commands: list[str] | None = None,
    latest: bool = False,
    mode: Mode = "continue",
) -> Task:
    """Send a finished or stopped task back for another run, with the operator's correction added to
    the approved plan (and, if given, new setup or test commands).

    `continue` (the default) picks up where the last attempt stopped: the next run applies its change,
    reuses its baseline, and gives the Coder only what failed (the review's comments, the regressions, the
    check's problems) instead of starting from the plan again. `restart` starts from the base commit. Either
    way the attempt is kept in the task's history (`result["attempts"]`, its diff in the task's folder)."""
    await mark_stale_runs()
    task, repo = await load_task(task_id)
    if task.status not in ("ready", "needs_operator", "failed", "published"):
        raise TaskError(f"Task {task_id} is {task.status}; it cannot be revised now.")
    combined = f"{task.note}\n\nRevision: {note}" if task.note else f"Revision: {note}"
    # A published task keeps its PR and claim, so review feedback becomes a follow-up commit on that PR.
    keep = {
        k: v
        for k, v in task.result.items()
        if k in ("brief", "map_commit", "pr_url", "claim_url", "published_diff", "branch")
    }
    if attempt := _attempt_record(task, task_dir(repo.full_name, task.id)):
        keep["attempts"] = [*task.result.get("attempts", []), attempt]
    if mode == "continue" and (resume := resume_point(task)):
        keep["resume"] = resume
    plan = dict(task.plan)
    if setup_commands:
        plan["setup_commands"] = setup_commands
    if test_commands:
        plan["test_commands"] = test_commands
    if why := run_blocker(plan):
        raise TaskError(why)
    if latest:  # upstream moved on (Gate 2 drift): redo the fix on the current default branch
        base = await latest_commit(repo.full_name)
        combined += f"\n\nRebased from {task.base_commit[:7]} to {base[:7]} (latest default branch)."
        await _save(task, base_commit=base)
    await _save(task, status="approved", note=combined, plan=plan, result=keep, diff=None, error=None)
    return task


def _last_failure(result: dict[str, Any]) -> str | None:
    """The check the last attempt failed last, from its Checks (plan, scope, test, review, suites)."""
    failed = [c for c in result.get("checks", []) if not c.get("ok")]
    return failed[-1]["check"] if failed else None


def resume_point(task: Task) -> dict[str, Any] | None:
    """What the next run needs to pick up where this attempt stopped, or None when there is nothing to keep."""
    diff = task.diff or task.result.get("partial_diff") or ""
    if not diff.strip():
        return None
    res = task.result
    stage = _last_failure(res)
    reviews = res.get("reviews") or []
    last_review = reviews[-1] if reviews else None
    if stage == "review" and last_review:
        fields = {k: v for k, v in last_review.items() if k not in ("round", "markdown")}
        todo = reviewer.feedback_for_coder(reviewer.Review.model_validate(fields))
    elif res.get("check") and not res["check"].get("passed"):
        todo = "The checks failed on the last attempt:\n\n" + FixCheck.model_validate(res["check"]).report()
    elif task.error:
        todo = f"The last attempt stopped: {task.error}"
    else:  # it finished; the operator's revision note says what to change
        todo = "The last attempt finished; the developer's revision note says what to change."
    return {
        "diff": diff,
        "base": task.base_commit,
        "baseline": res.get("baseline") or [],
        "stopped_at": stage or ("time or step limit" if task.error and "limit" in task.error else task.status),
        "todo": todo,
    }


def _attempt_record(task: Task, folder: Path) -> dict[str, Any] | None:
    """A finished or stopped attempt, kept when the task is revised: its summary here, its diff in a file."""
    diff = task.diff or task.result.get("partial_diff") or ""
    if not diff and not task.result.get("checks"):
        return None
    n = len(task.result.get("attempts", [])) + 1
    (folder / f"attempt-{n}.patch").write_text(diff)
    return {
        "n": n, "status": task.status, "error": task.error, "base": task.base_commit,
        "stopped_at": _last_failure(task.result), "checks": task.result.get("checks", []),
        "reviews": [{"round": r.get("round"), "verdict": r.get("verdict")} for r in task.result.get("reviews", [])],
        "cost_usd": task.cost_usd, "diff_file": f"attempt-{n}.patch", "lines": diff.count("\n"),
        "at": datetime.now(UTC).isoformat(),
    }  # fmt: skip


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


PUBLISHED_PATCH = "/tmp/ns-published.patch"  # outside the repo


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
    resume = result.pop("resume", None)  # used by this run only: a later revise records a new one
    await _save(task, status="working", error=None)
    await events.start_run(task.run_id, repo.id)
    beat = start_heartbeat(task.id)
    box: Sandbox | None = None
    coder: CoderRun | None = None
    try:
        await events.record(task.run_id, "node_start", "setup", node="setup")
        env = plan.get("environment") or {}
        image = await ensure_env_image(env.get("system_packages", []), env.get("tools", []))
        box, cached = await prepare_repo(repo.full_name, task.base_commit, plan["setup_commands"], image=image)
        kept = [CommandResult.model_validate(b) for b in (resume or {}).get("baseline", [])]
        if resume and resume["base"] == task.base_commit and [b.command for b in kept] == plan["test_commands"]:
            # Same commit, same commands: the last attempt's baseline still holds, so it is not run again
            baseline = kept
            await events.record(task.run_id, events.FIXED, "Baseline reused from the last attempt (same commit)")
        else:
            await events.record(task.run_id, "node_start", "baseline", node="baseline")
            baseline = await run_commands(box, plan["test_commands"], note="baseline, before any change")
            await save_recipe(repo.id, task.base_commit, plan["setup_commands"], baseline)
        result["baseline"] = [b.model_dump() for b in baseline]
        if resume:  # pick up where the last attempt stopped: its change goes back in before the Coder starts
            await box.write_file(PUBLISHED_PATCH, resume["diff"])
            applied = await box.exec(f"git apply --3way --whitespace=nowarn {PUBLISHED_PATCH}")
            if applied.ok:
                await events.record(
                    task.run_id, events.FIXED,
                    f"Continuing from the last attempt, which stopped at {resume['stopped_at']}: its change is applied",
                )  # fmt: skip
            else:
                await events.record(
                    task.run_id, events.CAUGHT,
                    f"The last attempt's change no longer applies to {task.base_commit[:7]}: starting from the plan",
                )  # fmt: skip
                # tracked files only: untracked ones include the setup's own (a venv, tool caches)
                await box.exec(f"git reset --quiet && git checkout --quiet --force {task.base_commit} -- .")
                resume = None
        elif published := task.result.get("published_diff"):
            # A revision of a published PR starts from the PR as it is, so the Coder changes only what the
            # revision asks; verification still compares the whole change with the base commit.
            await box.write_file(PUBLISHED_PATCH, published)
            applied = await box.exec(f"git apply --3way --whitespace=nowarn {PUBLISHED_PATCH}")
            if not applied.ok:
                raise SandboxError(f"the published PR no longer applies to {task.base_commit[:7]}: {applied.tail(400)}")

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
        # The issue's reproduction, when it failed on the code before any change: the fix must make it pass.
        reproduced = plan.get("reproduced") or {}
        coder.reproduction = plan.get("reproduction") if reproduced and not reproduced.get("ok") else None
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
            if coder.reproduction:
                instruction += (
                    " The plan's reproduction script fails on the current code; verification runs it again "
                    f"(`{coder.reproduction['command']}`) and it must pass with your fix."
                )
        if resume:  # only what failed, not the whole plan again: no re-exploring, fewer tokens
            instruction = (
                f"Your previous attempt is already applied in the working tree (`git diff {task.base_commit[:12]}` "
                f"shows it). It stopped at {resume['stopped_at']}. Keep what is right, fix only what is below and "
                "anything the developer's revision note in the plan asks, then call submit.\n\n" + resume["todo"]
            )
        coder.scope = Scope(
            planned=[f["path"] for f in plan.get("files_to_change", [])], size=plan.get("size", "medium")
        )
        # The Checks: each gate as it runs, cheapest first (verify.py), saved as it goes so the page shows it live.
        checks = coder.checks
        if pc := plan.get("plan_check"):
            musts = [f for f in pc.get("findings", []) if f.get("severity") == "must"]
            replanned = " Re-planned once for its must-fix findings." if pc.get("revised") else ""
            # Passed when nothing was must-fix, or the revision addressed every must-fix finding on re-check
            # (plans made before the re-check: the revision alone)
            fixed = all(r.get("addressed") for r in pc["recheck"]) if pc.get("recheck") else bool(pc.get("revised"))
            checks.append(step("plan", not musts or fixed, 0, 0, pc.get("summary", "") + replanned))
        result["checks"] = checks

        async def checkpoint() -> None:
            await _save(task, result={**result, "checks": checks})

        reviews: list[dict[str, Any]] = []
        status, why = "needs_operator", ""
        for round_no in range(1, settings.review_rounds + 2):
            coder.round = round_no
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
            await checkpoint()
            if coder.check is None or not coder.check.passed:
                why = coder.stopped or "verification did not pass"
                break
            await events.record(task.run_id, "node_start", f"review round {round_no}", node="review")
            started = time.monotonic()
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
            approved = (
                rv.verdict == "approve" and not reviewer.blocking(rv) and not any(c.needs_change for c in rv.related)
            )
            checks.append(step("review", approved, round_no, time.monotonic() - started, rv.summary))
            await checkpoint()
            if not approved:
                await events.record(task.run_id, events.CAUGHT, "Review asked for changes: sent back to the Coder")
                instruction = reviewer.feedback_for_coder(rv)
                why = "the review still asks for changes after the last round"
                continue
            # The slowest check runs last, once: only on a change that is in scope, proven and approved.
            await events.record(task.run_id, "node_start", f"full suites round {round_no}", node="suites")
            started = time.monotonic()
            await check_suites(coder.check, box, plan["test_commands"], baseline)
            ran = coder.check
            detail = "; ".join(ran.problems) or f"{len(ran.suite)} commands, no regression"
            if ran.already_failing:
                detail += f" ({len(ran.already_failing)} already failing before the change)"
            checks.append(step("suites", ran.passed, round_no, time.monotonic() - started, detail))
            await checkpoint()
            if ran.passed:
                status = "ready"
                break
            await events.record(
                task.run_id, events.CAUGHT, f"Full suites found regressions ({detail}): sent back to the Coder"
            )
            instruction = (
                "The Reviewer approved the change, but the full suites found regressions: commands that passed "
                "before the change fail now. Fix them without weakening any test, then call submit again.\n\n"
                + ran.report()
            )
            why = "the full suites still found regressions after the last round"

        final_diff = await diff(box, task.base_commit)
        if "relevant_files" in plan:  # plans made before the list was kept cannot be checked
            planned = [f["path"] for f in plan.get("files_to_change", [])]
            result["context_check"] = coverage(final_diff, plan["relevant_files"], planned)
            if missed := result["context_check"]["missed"]:
                await events.record(
                    task.run_id,
                    "warning",
                    "the fix changed files its planning context did not hold: " + ", ".join(missed),
                )
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
    except (BudgetExceeded, KillSwitchActive, SandboxError, ModelAPIError, ModelConfigError) as e:
        # Limits, the sandbox, or the model account (no credits, a bad key): the work so far is kept, and a
        # revise runs it again once the cause is fixed.
        if box:
            result["partial_diff"] = await diff(box, task.base_commit)
        if coder and coder.check:
            result["check"] = coder.check.model_dump()
        why = f"{type(e).__name__}: {e}"
        if isinstance(e, ModelAPIError) and e.status in (401, 402, 403):
            why = (
                f"OpenRouter refused the call ({e.status}: {e.message[:200]}). Fix the key or add credits, then revise."
            )
        await _save(task, status="needs_operator", result=result, error=why, cost_usd=await task_cost(task))
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


async def repo_conventions(full_name: str) -> tuple[str, list[str]]:
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
    template, titles = await repo_conventions(repo.full_name)
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
        "template_headings": publish.template_headings(template),
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
    if task.result.get("claim_url"):
        raise TaskError(f"Already posted: {task.result['claim_url']}")
    if not confirm:
        return f"Would post on {repo.full_name}#{task.issue_number}:\n\n{text}"
    # Marked before posting, so a second click (or a second request) finds it taken and posts nothing.
    async with session_scope() as s:
        marked = await s.execute(
            update(Task)
            .where(Task.id == task.id, Task.result["claim_url"].as_string().is_(None))
            .values(result={**task.result, "claim_url": "posting"})
        )
        claimed = cast(CursorResult[Any], marked).rowcount
    if not claimed:
        raise TaskError("The claim comment is already posted or being posted.")
    try:
        url = await publish.post_claim(repo, task, text)
    except BaseException:
        await _save(task, result={k: v for k, v in task.result.items() if k != "claim_url"})
        raise
    await _save(task, result={**task.result, "claim_url": url})
    return url


# What Gate 2 lets the operator change before publishing.
GATE2_EDITS = ("title", "body", "commit_message", "attribution", "branch")


async def gate2(
    task_id: int,
    *,
    confirm: bool,
    edits: dict[str, str] | None = None,
    remember: bool = False,
    replies: dict[str, str] | None = None,
    post_replies: bool = False,
) -> dict[str, Any]:
    """Prepare the PR on the current upstream branch and show exactly what would go out: title, body,
    commit message with its attribution line, branch. `edits` change any of those (kept for next time);
    `remember` stores the attribution and branch prefix for this repo. With `confirm`, push and open it."""
    task, repo = await load_task(task_id)
    if task.status != "ready":
        raise TaskError(f"Task {task_id} is {task.status}; only a ready task can be published.")
    edits = {k: v for k, v in (edits or {}).items() if k in GATE2_EDITS and v is not None}
    if task.result.get("pr_url"):  # revised after publishing: the change goes onto that PR, not a new one
        return await _follow_up(task, repo, confirm=confirm, edits=edits, replies=replies, post_replies=post_replies)
    pr = dict(task.result.get("pr") or await draft_pr(task, repo))
    pr["commit_message"] = publish.strip_attribution(pr["commit_message"])  # drafts before Gate 2 showed it
    pr.update({k: edits[k] for k in ("title", "body", "commit_message") if k in edits})
    attribution = edits.get("attribution", publish.attribution_for(repo)).strip()
    branch = edits.get("branch", "").strip() or publish.branch_name(task, repo)
    if edits:
        await _save(task, result={**task.result, "pr": pr})
    if remember:
        async with session_scope() as s:
            row = await s.get(Repo, repo.id)
            assert row is not None
            row.publish = {**(row.publish or {}), "attribution": attribution,
                           "branch_prefix": branch.rsplit("/", 1)[0] if "/" in branch else ""}  # fmt: skip

    blockers = await publish.freshness(repo.full_name, task)
    if missing := publish.missing_sections(pr["body"], pr.get("template_headings") or []):
        blockers.append("the PR body is missing these sections of the repo's PR template: " + ", ".join(missing))
    warnings = []
    check = FixCheck.model_validate(task.result["check"]) if task.result.get("check") else None
    if check and (failing := [r.command for r in check.suite if not r.ok]):
        warnings.append(
            "The Testing section shows failing suites (they failed before the change too): " + ", ".join(failing)
        )
    if attribution:
        warnings.append(f'The commit ends with the line "{attribution}". Clear it below to leave it out.')
    from nightshift.worker.understanding import gate_warning  # it builds on this module

    if understood := gate_warning(task.result):  # warns, never blocks (the agreed product rule)
        warnings.append(understood)

    prepared = await publish.prepare(repo, task, task_dir(repo.full_name, task.id) / "publish", branch=branch)
    info = publish.summary(prepared, pr["title"], pr["body"]) | {
        "blockers": blockers, "warnings": warnings, "commit_message": pr["commit_message"],
        "attribution": attribution, "template_headings": pr.get("template_headings") or [],
    }  # fmt: skip
    if not confirm or blockers or prepared.drift:
        return info | {"published": False}
    message = publish.with_attribution(pr["commit_message"], attribution)
    url = await publish.publish(repo, task, prepared, title=pr["title"], body=pr["body"], message=message)
    result = {**task.result, "pr": pr, "pr_url": url, "published_diff": task.diff, "branch": prepared.branch}
    await _save(task, status="published", result=result)
    return info | {"published": True, "url": url}


async def _follow_up(
    task: Task,
    repo: Repo,
    *,
    confirm: bool,
    edits: dict[str, str],
    replies: dict[str, str] | None = None,
    post_replies: bool = False,
) -> dict[str, Any]:
    """Gate 2 for a published task that was revised: one more commit on its PR's branch, holding the
    difference between the published fix and the revised one. With a `body` edit, the PR's description
    is replaced too. When the revision addressed review comments, a reply to each is drafted; `replies`
    holds the operator's edits, and `post_replies` posts them after the push."""
    try:
        pr = await publish.pull_info(repo, task.result["pr_url"])
        published = task.result.get("published_diff") or await publish.pull_diff(repo, pr)
    except publish.PublishError as e:
        raise TaskError(str(e)) from e
    revision = (task.note or "").rsplit("Revision:", 1)[-1].strip()
    default = "Address review feedback" + (f"\n\n{revision}" if revision else "")
    message = edits.get("commit_message") or task.result.get("follow_up_message") or default
    attribution = edits.get("attribution", publish.attribution_for(repo)).strip()
    if "commit_message" in edits:
        await _save(task, result={**task.result, "follow_up_message": message})
    blockers = await publish.freshness(repo.full_name, task)
    login = get_settings().github_login
    if pr.state != "OPEN":
        blockers.append(f"the pull request is {pr.state.lower()}")
    if not login or pr.head_owner.lower() != login.lower():
        blockers.append(
            f"the PR's branch is on {pr.head_owner}'s fork, not yours ({login or 'NIGHTSHIFT_GITHUB_LOGIN unset'})"
        )
    warnings = [f'The commit ends with the line "{attribution}". Clear it to leave it out.'] if attribution else []
    info: dict[str, Any] = {
        "mode": "follow_up", "url": pr.url, "branch": pr.head_branch, "base": "", "head": "", "drift": [],
        "files": [], "title": (task.result.get("pr") or {}).get("title", ""),
        "body": (task.result.get("pr") or {}).get("body", ""), "commit_message": message,
        "attribution": attribution, "template_headings": [], "blockers": blockers,
        "warnings": warnings,
        "published": False,
    }  # fmt: skip
    if blockers:
        return info
    addressing = [feedback.ReviewComment.model_validate(c) for c in task.result.get("addressing") or []]
    try:
        prepared = await publish.prepare_follow_up(
            repo, task, pr, published, task_dir(repo.full_name, task.id) / "publish"
        )
    except publish.PublishError as e:
        if "nothing to push" not in str(e) or not addressing:
            raise
        # The comments asked no code change (a question, a confirmation): answer them, push nothing.
        return await _replies_only(task, repo, pr.number, addressing, info, confirm, replies, post_replies)
    info["files"] = prepared.files
    drafted: dict[str, str] = dict(task.result.get("replies") or {})
    if addressing and not drafted:
        change = (await publish._run("git", "diff", "--cached", cwd=Path(prepared.workdir))) or ""
        llm = LLMGateway(task.run_id, repo.id, run_budget_usd=get_settings().task_budget_usd)
        drafted = await feedback.draft_replies(llm, addressing, change)
    if replies:
        drafted.update({k: v for k, v in replies.items() if k in {c.id for c in addressing}})
    if drafted != (task.result.get("replies") or {}):
        await _save(task, result={**task.result, "replies": drafted})
    info["replies"] = [c.model_dump() | {"text": drafted.get(c.id, "")} for c in addressing]
    if not confirm:
        return info
    await publish.push_follow_up(repo, task, pr, prepared, message=publish.with_attribution(message, attribution))
    if "body" in edits:
        await publish.update_body(repo, pr, edits["body"])
    posted = await feedback.post_replies(repo, pr.number, addressing, drafted) if post_replies else []
    done = sorted({*task.result.get("addressed", []), *(c.id for c in addressing)})
    result = {**task.result, "published_diff": task.diff, "follow_up_message": None, "addressed": done,
              "addressing": [], "replies": {}, "reply_urls": posted}  # fmt: skip
    await _save(task, status="published", result=result)
    return info | {"published": True, "reply_urls": posted}


async def _replies_only(
    task: Task,
    repo: Repo,
    number: int,
    addressing: list[feedback.ReviewComment],
    info: dict[str, Any],
    confirm: bool,
    replies: dict[str, str] | None,
    post_replies: bool,
) -> dict[str, Any]:
    drafted: dict[str, str] = dict(task.result.get("replies") or {})
    if not drafted:
        llm = LLMGateway(task.run_id, repo.id, run_budget_usd=get_settings().task_budget_usd)
        drafted = await feedback.draft_replies(llm, addressing, "(no code change)")
    drafted.update({k: v for k, v in (replies or {}).items() if k in {c.id for c in addressing}})
    await _save(task, result={**task.result, "replies": drafted})
    info |= {"mode": "replies_only", "files": [], "warnings": [],  # no commit: no attribution line
             "replies": [c.model_dump() | {"text": drafted.get(c.id, "")} for c in addressing]}  # fmt: skip
    if not confirm:
        return info
    posted = await feedback.post_replies(repo, number, addressing, drafted) if post_replies else []
    done = sorted({*task.result.get("addressed", []), *(c.id for c in addressing)})
    result = {**task.result, "addressed": done, "addressing": [], "replies": {}, "reply_urls": posted}
    await _save(task, status="published", result=result)
    return info | {"published": True, "reply_urls": posted}


async def draft_reply_only(task_id: int, comment_ids: list[str]) -> list[dict[str, Any]]:
    """Replies to comments that ask no code change (a question, a confirmation), drafted without a run."""
    task, repo = await load_task(task_id)
    chosen = [c for c in await review_feedback(task_id) if c.id in set(comment_ids)]
    if not chosen:
        raise TaskError("Choose at least one comment to reply to.")
    llm = LLMGateway(task.run_id, repo.id, run_budget_usd=get_settings().task_budget_usd)
    drafted = await feedback.draft_replies(llm, chosen, "(no code change)")
    await _save(task, result={**task.result, "reply_only": {"comments": [c.model_dump() for c in chosen],
                                                             "replies": drafted}})  # fmt: skip
    return [c.model_dump() | {"text": drafted.get(c.id, "")} for c in chosen]


async def post_reply_only(task_id: int, replies: dict[str, str]) -> list[str]:
    """Post the operator's approved replies (from draft_reply_only), with their gh login."""
    task, repo = await load_task(task_id)
    pending = task.result.get("reply_only") or {}
    comments = [feedback.ReviewComment.model_validate(c) for c in pending.get("comments", [])]
    if not comments:
        raise TaskError("Draft the replies first.")
    texts = {c.id: (replies.get(c.id) if c.id in replies else pending["replies"].get(c.id, "")) for c in comments}
    number = int(str(task.result["pr_url"]).rstrip("/").rsplit("/", 1)[-1])
    try:
        posted = await feedback.post_replies(repo, number, comments, {k: v or "" for k, v in texts.items()})
    except publish.PublishError as e:
        raise TaskError(str(e)) from e
    done = sorted({*task.result.get("addressed", []), *(c.id for c in comments if (texts.get(c.id) or "").strip())})
    await _save(task, result={**task.result, "addressed": done, "reply_only": {}, "reply_urls": posted})
    return posted


async def attach_pr(task_id: int, url: str) -> Task:
    """Link a PR opened by hand to its task: the task becomes published, and the PR's current diff is
    kept as what was published, so a revision can follow up on it."""
    task, repo = await load_task(task_id)
    if task.status in ("planning", "working", "planned", "approved"):
        raise TaskError(f"Task {task_id} is {task.status}; attach a PR once it has a fix.")
    try:
        patch, base, pr = await pr_diff_and_base(repo, url)
    except (publish.PublishError, GitHubError) as e:
        raise TaskError(str(e)) from e
    result = {**task.result, "pr_url": pr.url, "published_diff": patch, "branch": pr.head_branch}
    await _save(task, status="published", base_commit=base, result=result)
    return task


# ---------- review feedback ----------


async def pr_diff_and_base(repo: Repo, url: str) -> tuple[str, str, publish.PullInfo]:
    """The PR's changes as one diff, and the commit that diff is against (where its branch left the base
    branch). The two belong together: a task that takes the diff from GitHub must also take that base."""
    pr = await publish.pull_info(repo, url)
    gh = GitHubClient()
    try:
        raw = await gh.rest_get(f"/repos/{repo.full_name}/pulls/{pr.number}")
        compare = await gh.rest_get(f"/repos/{repo.full_name}/compare/{raw['base']['sha']}...{raw['head']['sha']}")
    finally:
        await gh.aclose()
    return await publish.pull_diff(repo, pr), compare["merge_base_commit"]["sha"], pr


async def review_feedback(task_id: int) -> list[feedback.ReviewComment]:
    """The open feedback on the task's published PR, leaving out what an earlier revision addressed."""
    task, repo = await load_task(task_id)
    if not task.result.get("pr_url"):
        raise TaskError(f"Task {task_id} has no pull request yet.")
    number = int(str(task.result["pr_url"]).rstrip("/").rsplit("/", 1)[-1])
    try:
        return await feedback.pr_feedback(repo.full_name, number, skip=set(task.result.get("addressed", [])))
    except GitHubError as e:
        raise TaskError(f"Cannot read the PR's feedback: {e}") from e


async def address_review(task_id: int, comment_ids: list[str], note: str = "") -> Task:
    """Revise the task to address the chosen review comments (and the operator's note). The run starts from
    the PR as published; Gate 2 then pushes one follow-up commit and drafts a reply to each comment."""
    chosen = [c for c in await review_feedback(task_id) if c.id in set(comment_ids)]
    if not chosen and not note.strip():
        raise TaskError("Choose at least one comment, or write a note.")
    task, repo = await load_task(task_id)
    if not task.result.get("published_diff"):  # published before NightShift kept it: take it from the PR
        try:
            patch, base, _ = await pr_diff_and_base(repo, task.result["pr_url"])
        except (publish.PublishError, GitHubError) as e:
            raise TaskError(str(e)) from e
        await _save(task, base_commit=base, result={**task.result, "published_diff": patch})
    task = await revise(task_id, feedback.revision_note(chosen, note))
    message = feedback.commit_message(chosen, note)
    await _save(task, result={**task.result, "addressing": [c.model_dump() for c in chosen], "replies": {},
                              "follow_up_message": message})  # fmt: skip
    return task


async def task_from_pr(url: str) -> Task:
    """A published task for a PR of yours that NightShift did not open, so its review feedback can be
    addressed here. The plan is the PR itself; the environment is the repo's (Setup agent)."""
    from nightshift.worker.prcheck import fetch_pull, linked_issue_number  # the checker imports this module

    pr = await fetch_pull(url)
    login = get_settings().github_login
    if not login or pr.author.lower() != login.lower():
        raise TaskError(f"{pr.url} is by {pr.author}; only your own pull requests can be followed up here.")
    if pr.state != "open":
        raise TaskError(f"{pr.url} is {pr.state}.")
    async with session_scope() as s:
        repo = await get_or_create_repo(s, pr.repo)
    try:
        patch, base, info = await pr_diff_and_base(repo, pr.url)
    except (publish.PublishError, GitHubError) as e:
        raise TaskError(str(e)) from e
    number = linked_issue_number(pr)
    issue = await fetch_issue(pr.repo, number) if number else None
    llm = LLMGateway(f"import-{repo.id}-{pr.number}", repo.id, run_budget_usd=get_settings().setup_budget_usd)
    env = await environment_for(llm, repo, base)
    if env.status != "ready":
        raise TaskError(f"The repo's environment is {env.status}: {env.blocker or env.detail}")
    markdown = (
        f"# Plan: follow up on {pr.repo}#{pr.number} {pr.title}\n\nImported from the pull request: its "
        f"changes are the work so far.\n\n{pr.body.strip()}"
    )
    plan = {
        "markdown": markdown, "actionable": True, "kind": "code", "setup_commands": env.setup_commands,
        "test_commands": env.test_commands, "test_plan": {"file": "", "asserts": "", "targeted_command": ""},
        "environment": {"system_packages": env.system_packages, "tools": env.tools}, "models": {},
    }  # fmt: skip
    issue_data = issue.model_dump(mode="json") if issue else {
        "number": pr.number, "title": pr.title, "body": pr.body, "state": "open", "author": pr.author,
        "labels": [], "assignees": [], "comments": [], "linked_prs": [], "url": pr.url,
    }  # fmt: skip
    async with session_scope() as s:
        task = Task(
            repo_id=repo.id, issue_number=number or pr.number, status="published", base_commit=base,
            issue=issue_data, plan=plan, diff=patch,
            result={"pr_url": pr.url, "published_diff": patch, "branch": info.head_branch},
        )  # fmt: skip
        s.add(task)
    return task
