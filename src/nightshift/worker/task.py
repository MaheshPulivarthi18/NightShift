"""A contribution task from issue to reviewed fix: plan, Gate 1, then Coder and Reviewer in a sandbox.

    plan_task:  fresh issue -> sandbox -> brief (L0), map (L1), relevant files (L2) -> Planner -> "planned"
    approve / reject:  Gate 1, the operator's decision
    run_task:   setup -> baseline tests -> Coder -> verification -> Reviewer (-> Coder again, up to the
                review-round limit) -> "ready" with a diff, or "needs_operator" with the work saved

Nothing here writes to GitHub. Limits per task: settings.task_budget_usd (planning included),
task_minutes and coder_max_steps.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select

from nightshift import events
from nightshift.config import get_settings
from nightshift.context import archmap
from nightshift.context.change import change_context
from nightshift.context.change import to_markdown as change_markdown
from nightshift.db import LLMCall, Repo, RepoContext, Task, session_scope
from nightshift.health.agents import RepoBrief
from nightshift.health.graph import repo_brief
from nightshift.llm import BudgetExceeded, KillSwitchActive, LLMGateway
from nightshift.memory import get_or_create_repo
from nightshift.sandbox.docker import Sandbox, SandboxError, prepare_repo
from nightshift.worker import planner, reviewer
from nightshift.worker.coder import CoderRun
from nightshift.worker.issue import IssueView, fetch_issue
from nightshift.worker.relevant import relevant_files
from nightshift.worker.verify import CommandResult, diff, run_commands

log = structlog.get_logger()

RECIPE = "recipe"


class TaskError(RuntimeError):
    pass


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


async def plan_task(full_name: str, number: int, *, base: str | None = None, rehearsal: bool = False) -> Task:
    """Plan a fix for an issue and stop at Gate 1. A rehearsal may plan an issue someone already fixed."""
    settings = get_settings()
    issue = await fetch_issue(full_name, number)
    blockers = issue.blockers(settings.github_login or None)
    if blockers and not rehearsal:
        raise TaskError(f"#{number} is not free: " + "; ".join(blockers))

    async with session_scope() as s:
        repo = await get_or_create_repo(s, full_name)
        task = Task(
            repo_id=repo.id, issue_number=number, status="planning", base_commit="", rehearsal=rehearsal,
            issue=issue.model_dump(mode="json"), plan={},
        )  # fmt: skip
        s.add(task)
    await events.start_run(task.run_id, repo.id)
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
        tracked = set((await box.exec("git ls-files")).stdout.splitlines())

        await events.record(task.run_id, "node_start", "plan", node="plan")
        context = (
            f"<untrusted_github_data>\n<issue>\n{issue_text(issue)}\n</issue>\n</untrusted_github_data>\n\n"
            f"<repo_brief>\n{brief_text(brief)}\n</repo_brief>\n\n"
            f"<architecture_map>\n{archmap.to_markdown(amap)}\n</architecture_map>\n\n"
            f"<relevant_files>\n{l2}\n</relevant_files>\n\n"
            f"<commands_that_worked_before>\n{recipe or 'none yet'}\n</commands_that_worked_before>"
        )
        plan = await planner.make_plan(llm, repo=full_name, context=context)
        warnings = [f"`{p}` does not exist in the repo" for p in planner.unknown_paths(plan, tracked)]
        warnings += [f"rehearsal: {b}" for b in blockers]
        markdown = planner.to_markdown(plan, title=f"{full_name}#{number} {issue.title}", warnings=warnings)
        (task_dir(full_name, task.id) / "plan.md").write_text(markdown)
        await _save(
            task,
            status="planned",
            base_commit=commit,
            plan={**plan.model_dump(mode="json"), "warnings": warnings, "markdown": markdown},
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
        if box:
            await box.remove()


async def decide(task_id: int, approve: bool, note: str | None = None, **overrides: list[str]) -> Task:
    """Gate 1. `overrides` may replace the plan's setup_commands or test_commands."""
    task, _ = await load_task(task_id)
    if task.status != "planned":
        raise TaskError(f"Task {task_id} is {task.status}, not waiting for approval.")
    plan = dict(task.plan)
    plan.update({k: v for k, v in overrides.items() if v})
    if approve:
        await _save(task, status="approved", plan=plan, note=note, approved_at=datetime.now(UTC))
    else:
        await _save(task, status="rejected", note=note)
    return task


# ---------- work ----------


async def run_task(task_id: int) -> Task:
    """Set up, record a baseline, then Coder and Reviewer rounds until the fix is verified and approved."""
    settings = get_settings()
    task, repo = await load_task(task_id)
    if task.status != "approved":
        raise TaskError(f"Task {task_id} is {task.status}; only an approved plan can run.")
    plan = task.plan
    issue = IssueView.model_validate(task.issue)
    folder = task_dir(repo.full_name, task.id)
    deadline = time.monotonic() + settings.task_minutes * 60
    llm = LLMGateway(task.run_id, repo.id, run_budget_usd=settings.task_budget_usd)
    result: dict[str, Any] = dict(task.result)
    await _save(task, status="working", error=None)
    await events.start_run(task.run_id, repo.id)
    box: Sandbox | None = None
    try:
        await events.record(task.run_id, "node_start", "setup", node="setup")
        box, cached = await prepare_repo(repo.full_name, task.base_commit, plan["setup_commands"])
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
        coder = CoderRun(box, llm, task.base_commit, plan["test_commands"], baseline, context)
        instruction = (
            "Fix the issue following the approved plan. Start by reading the files it names. "
            f"The plan's targeted test command is `{plan['test_plan']['targeted_command']}`."
        )
        reviews: list[dict[str, Any]] = []
        status, why = "needs_operator", ""
        for round_no in range(1, settings.review_rounds + 2):
            await events.record(task.run_id, "node_start", f"coder round {round_no}", node="coder")
            await coder.work(instruction, deadline)
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
        await _save(task, status="failed", result=result, error=f"{type(e).__name__}: {e}",
                    cost_usd=await task_cost(task))  # fmt: skip
        await events.finish_run(task.run_id, "failed", error=str(e))
        raise
    finally:
        if box:
            await box.remove()
