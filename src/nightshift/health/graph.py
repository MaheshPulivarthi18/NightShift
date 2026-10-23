"""The Health Report pipeline as a LangGraph state graph.

maintainer repos:  fetch -> link -> analyse_groups -> assess_prs -> verify -> scout -> report
contributor repos: fetch -> link -> scout -> contributor_report

State holds only small, JSON-friendly values (ids, verdicts); bulky data stays in
Postgres. LangGraph checkpoints the state after each node, so a run that stops on a
crash, a budget cap or the kill switch resumes from the last finished node. Agent
results are also cached by input hash, so a resumed node does not pay twice.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypedDict, cast

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from nightshift import events
from nightshift.config import get_settings
from nightshift.db import Item, LLMCall, Repo, session_scope
from nightshift.github import FETCH_VERSION, GitHubClient, GitHubError
from nightshift.health import agents
from nightshift.health.brief import briefer_input, missing_paths
from nightshift.health.contributor import maintainers, prs_by_issue, pulse, work_status
from nightshift.health.linker import Edge, candidate_groups, explicit_edges
from nightshift.health.report import (
    build_findings_for_verification,
    is_opportunity,
    render_contributor_report,
    render_report,
    save_report,
)
from nightshift.health.signals import pr_signals
from nightshift.llm import AgentOutputError, BudgetExceeded, KillSwitchActive, LLMGateway, ModelRefused
from nightshift.memory import (
    feedback_examples,
    get_or_create_repo,
    nearest,
    refresh_embeddings,
    upsert_items,
)

log = structlog.get_logger()

# Idle PRs with no other signal still go to the assessor: they may be stuck on a decision.
ASSESS_IF_IDLE_DAYS = 30
SIMILAR_NEIGHBOURS = 5
VERIFY_CHUNK = 8
SCOUT_CHUNK = 8


class HealthState(TypedDict, total=False):
    run_id: str
    repo: str
    repo_id: int
    role: str
    stats: dict[str, Any]
    similar: list[list[Any]]
    groups: list[dict[str, Any]]
    clusters: list[dict[str, Any]]
    verdicts: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    report_path: str
    pulse: dict[str, Any]
    opportunities: list[dict[str, Any]]
    worked_on: list[dict[str, Any]]
    brief: dict[str, Any] | None


async def _bounded[T](tasks: list[Callable[[], Awaitable[T]]]) -> tuple[list[T], int]:
    """Run agent calls with limited concurrency. Returns the results and how many items were skipped.

    A refusal or unparsable answer for one item skips that item instead of failing the run;
    budget, kill switch and provider errors still stop it (the run can then be resumed).
    """
    sem = asyncio.Semaphore(get_settings().llm_concurrency)

    async def run(task: Callable[[], Awaitable[T]]) -> T | None:
        async with sem:
            try:
                return await task()
            except (ModelRefused, AgentOutputError) as e:
                log.warning("agent_item_skipped", error=str(e))
                return None

    results = await asyncio.gather(*(run(t) for t in tasks))
    ok = [r for r in results if r is not None]
    return ok, len(results) - len(ok)


async def _load(repo_id: int) -> list[Item]:
    async with session_scope() as s:
        return list((await s.execute(select(Item).where(Item.repo_id == repo_id))).scalars())


# ---------- nodes ----------


async def fetch(state: HealthState) -> HealthState:
    settings = get_settings()
    gh = GitHubClient()
    try:
        async with session_scope() as s:
            repo = await get_or_create_repo(s, state["repo"])
            # Items stored by an older fetcher lack newer fields: refetch everything once.
            since = repo.last_fetched_at if repo.fetch_version >= FETCH_VERSION else None
            repo_id, role = repo.id, repo.role
        raw, truncated = await gh.fetch_items(
            state["repo"], since, settings.closed_lookback_days, settings.max_items_per_run
        )
    finally:
        await gh.aclose()
    async with session_scope() as s:
        stored = await s.get(Repo, repo_id)
        assert stored is not None
        changed = await upsert_items(s, stored, raw)
        stored.fetch_version = FETCH_VERSION
    async with session_scope() as s:
        embedded = await refresh_embeddings(s, repo_id)
    log.info("fetched", repo=state["repo"], fetched=len(raw), changed=changed, embedded=embedded, truncated=truncated)
    stats = {"fetched": len(raw), "changed": changed, "first_run": since is None, "truncated": truncated}
    return {"repo_id": repo_id, "role": role, "stats": stats}


async def link(state: HealthState) -> HealthState:
    settings = get_settings()
    items = await _load(state["repo_id"])
    open_items = [it for it in items if it.state == "OPEN"]
    edges: list[Edge] = explicit_edges(items)
    async with session_scope() as s:
        for it in open_items:
            refreshed = await s.get(Item, it.id)
            assert refreshed is not None
            for other, sim in await nearest(s, refreshed, SIMILAR_NEIGHBOURS):
                if sim >= settings.similarity_threshold:
                    edges.append((it.number, other.number, f"similar:{sim:.3f}"))
    groups = candidate_groups({it.number for it in open_items}, edges, settings.max_group_size)
    stats = {**state["stats"], "open_items": len(open_items), "candidate_groups": len(groups)}
    similar = [[a, b, why] for a, b, why in edges if why.startswith("similar")]
    return {"groups": groups, "similar": similar, "stats": stats}


async def analyse_groups(state: HealthState) -> HealthState:
    llm = LLMGateway(state["run_id"], state["repo_id"])
    by_number = {it.number: it for it in await _load(state["repo_id"])}
    async with session_scope() as s:
        feedback = await feedback_examples(s, state["repo_id"])

    def task(group: dict[str, Any]) -> Callable[[], Awaitable[agents.RelevanceResult]]:
        members = [by_number[n] for n in group["numbers"] if n in by_number]
        return lambda: agents.analyse_relevance(llm, state["repo"], members, group["evidence"], feedback)

    results, skipped = await _bounded([task(g) for g in state["groups"]])
    # A cluster of only closed/merged items is history, not something to act on.
    clusters = [
        c.model_dump()
        for r in results
        for c in r.clusters
        if any(n in by_number and by_number[n].state == "OPEN" for n in c.numbers)
    ]
    return {"clusters": clusters, "stats": {**state["stats"], "skipped_groups": skipped}}


async def assess_prs(state: HealthState) -> HealthState:
    settings = get_settings()
    llm = LLMGateway(state["run_id"], state["repo_id"])
    by_number = {it.number: it for it in await _load(state["repo_id"])}
    async with session_scope() as s:
        feedback = await feedback_examples(s, state["repo_id"])

    group_of: dict[int, list[int]] = {}
    for g in state["groups"]:
        for n in g["numbers"]:
            group_of.setdefault(n, []).extend(x for x in g["numbers"] if x != n)

    verdicts: list[dict[str, Any]] = []
    tasks: list[Callable[[], Awaitable[dict[str, Any]]]] = []
    for pr in sorted(
        (it for it in by_number.values() if it.kind == "pr" and it.state == "OPEN"), key=lambda i: i.number
    ):
        sig = pr_signals(pr, by_number, settings.abandoned_after_days).to_dict()
        interesting = (
            sig["looks_ready"]
            or sig["looks_abandoned"]
            or sig["superseded_by"]
            or sig["linked_issues_not_planned"]
            or sig["linked_issues_fixed_elsewhere"]
            or sig["idle_days"] >= ASSESS_IF_IDLE_DAYS
        )
        if (sig["is_draft"] and not sig["looks_abandoned"]) or not interesting:
            verdicts.append(
                {
                    "number": pr.number,
                    "signals": sig,
                    "category": "active",
                    "reason_code": "none",
                    "confidence": "high",
                    "explanation": "Recent activity and no blocking signal.",
                    "evidence": [pr.ref],
                    "decision_question": None,
                    "closing_message": None,
                }
            )
            continue
        related_numbers = set(group_of.get(pr.number, [])) | set(sig["superseded_by"])
        related_numbers |= {i["issue"] for i in sig["linked_issues_fixed_elsewhere"]}
        related_numbers |= set(sig["linked_issues_not_planned"])
        related = [by_number[n] for n in sorted(related_numbers) if n in by_number]

        def make(
            pr: Item = pr, sig: dict[str, Any] = sig, related: list[Item] = related
        ) -> Callable[[], Awaitable[dict[str, Any]]]:
            async def run() -> dict[str, Any]:
                v = await agents.assess_pr(llm, state["repo"], pr, sig, related, feedback)
                return {"number": pr.number, "signals": sig, **v.model_dump()}

            return run

        tasks.append(make())

    assessed, skipped = await _bounded(tasks)
    verdicts += assessed
    verdicts.sort(key=lambda v: v["number"])
    return {"verdicts": verdicts, "stats": {**state["stats"], "skipped_prs": skipped}}


async def verify(state: HealthState) -> HealthState:
    llm = LLMGateway(state["run_id"], state["repo_id"])
    by_number = {it.number: it for it in await _load(state["repo_id"])}
    candidates = build_findings_for_verification(state["clusters"], state["verdicts"], by_number)

    chunks = [candidates[i : i + VERIFY_CHUNK] for i in range(0, len(candidates), VERIFY_CHUNK)]
    payload = [[{k: f[k] for k in ("key", "claim", "evidence")} for f in chunk] for chunk in chunks]

    def task(p: list[dict[str, Any]]) -> Callable[[], Awaitable[agents.VerifierResult]]:
        return lambda: agents.verify(llm, state["repo"], p)

    results, _ = await _bounded([task(p) for p in payload])
    checks = {c.key: c for r in results for c in r.checks}

    findings = []
    rejected = []
    for f in candidates:
        check = checks.get(f["key"])
        if check is None or not check.supported:
            problem = check.problem if check else "Not checked by the Verifier."
            log.info("finding_dropped", key=f["key"], problem=problem)
            rejected.append({"key": f["key"], "claim": f["claim"], "problem": problem})
            continue
        f["confidence"] = min(f["confidence"], check.confidence, key=["low", "medium", "high"].index)
        f["verifier_note"] = check.problem
        findings.append(f)
    stats = {**state["stats"], "findings_proposed": len(candidates), "findings_verified": len(findings)}
    return {"findings": findings, "rejected": rejected, "stats": stats}


async def report(state: HealthState) -> HealthState:
    async with session_scope() as s:
        q = select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.run_id == state["run_id"])
        cost = float((await s.execute(q)).scalar_one())
    by_number = {it.number: it for it in await _load(state["repo_id"])}
    stats = {**state["stats"], "cost_usd": round(cost, 4)}
    markdown = await render_report(
        state["repo"],
        state["repo_id"],
        state["findings"],
        state["clusters"],
        state["verdicts"],
        by_number,
        stats,
        run_id=state["run_id"],
        rejected=state.get("rejected", []),
        opportunities=state.get("opportunities", []),
        worked_on=state.get("worked_on", []),
    )
    path = await save_report(
        state["repo"],
        state["repo_id"],
        state["run_id"],
        {
            "findings": state["findings"],
            "clusters": state["clusters"],
            "verdicts": state["verdicts"],
            "opportunities": state.get("opportunities", []),
            "worked_on": state.get("worked_on", []),
        },
        markdown,
        cost,
    )
    return {"report_path": path, "stats": stats}


async def repo_brief(llm: LLMGateway, repo: str) -> tuple[agents.RepoBrief | None, set[str]]:
    """The Repo Briefer's summary plus the repo's file paths. A failure leaves the report without a brief."""
    gh = GitHubClient()
    try:
        docs = await gh.fetch_repo_docs(repo)
    except GitHubError as e:
        log.warning("repo_brief_unavailable", repo=repo, error=str(e))
        return None, set()
    finally:
        await gh.aclose()
    paths = set() if docs["tree_truncated"] else set(docs["paths"])
    try:
        return await agents.brief_repo(llm, repo, briefer_input(docs)), paths
    except (ModelRefused, AgentOutputError) as e:
        log.warning("repo_brief_failed", repo=repo, error=str(e))
        return None, paths


async def scout(state: HealthState) -> HealthState:
    """Contributor view: who is on which issue (code), and which free issues are worth taking (Scout)."""
    settings = get_settings()
    llm = LLMGateway(state["run_id"], state["repo_id"])
    brief, repo_paths = await repo_brief(llm, state["repo"])
    items = await _load(state["repo_id"])
    by_number = {it.number: it for it in items}
    linked = prs_by_issue(items)
    known = maintainers(items)

    similar_prs: dict[int, set[int]] = {}
    for a, b, _ in state.get("similar", []):
        for x, y in ((a, b), (b, a)):
            other = by_number.get(y)
            if other is not None and other.kind == "pr" and other.state == "OPEN":
                similar_prs.setdefault(x, set()).add(y)

    worked_on: list[dict[str, Any]] = []
    candidates: list[tuple[Item, dict[str, Any], list[Item]]] = []
    open_issues = sorted(
        (it for it in items if it.kind == "issue" and it.state == "OPEN" and not it.data.get("duplicate_of")),
        key=lambda it: it.updated_at,
        reverse=True,
    )
    for issue in open_issues:
        ws = work_status(issue, linked.get(issue.number, []), by_number)
        if ws.status == "taken":
            worked_on.append({"issue": issue.number, "prs": ws.active_prs, "source": "linked"})
            continue
        if ws.status == "claimed":
            worked_on.append({"issue": issue.number, "prs": [], "source": "claimed", "by": ws.claimed_by})
            continue
        related = [by_number[n] for n in sorted(similar_prs.get(issue.number, set()) - set(ws.stalled_prs))]
        candidates.append((issue, {"status": ws.status, "stalled_prs": ws.stalled_prs}, related[:3]))

    skipped_for_cap = max(0, len(candidates) - settings.max_opportunities)
    candidates = candidates[: settings.max_opportunities]
    batches = [candidates[i : i + SCOUT_CHUNK] for i in range(0, len(candidates), SCOUT_CHUNK)]

    def task(batch: list[tuple[Item, dict[str, Any], list[Item]]]) -> Callable[[], Awaitable[agents.ScoutResult]]:
        return lambda: agents.scout_issues(llm, state["repo"], batch, known, brief)

    results, skipped = await _bounded([task(b) for b in batches])
    status_of = {issue.number: st for issue, st, _ in candidates}
    opportunities = []
    for opp in (o for r in results for o in r.issues):
        if opp.already_addressed_by:
            worked_on.append({"issue": opp.number, "prs": opp.already_addressed_by, "source": "scout"})
            continue
        # Paths the Scout names that the repo does not have; shown so the reader double-checks them.
        unverified = missing_paths(opp.first_step, repo_paths) if repo_paths else []
        opportunities.append({**opp.model_dump(), **status_of[opp.number], "unverified_paths": unverified})

    stats = {
        **state["stats"],
        "open_issues": len(open_issues),
        "open_prs": sum(1 for it in items if it.kind == "pr" and it.state == "OPEN"),
        "issues_scouted": len(candidates),
        "issues_not_scouted": skipped_for_cap,
        "skipped_groups": skipped,
    }
    return {
        "pulse": pulse(items, settings.pulse_window_days).to_dict(),
        "opportunities": opportunities,
        "worked_on": worked_on,
        "brief": brief.model_dump() if brief else None,
        "stats": stats,
    }


async def contributor_report(state: HealthState) -> HealthState:
    async with session_scope() as s:
        q = select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.run_id == state["run_id"])
        cost = float((await s.execute(q)).scalar_one())
    by_number = {it.number: it for it in await _load(state["repo_id"])}
    stats = {**state["stats"], "cost_usd": round(cost, 4)}
    markdown = await render_contributor_report(
        state["repo"],
        state["repo_id"],
        state["pulse"],
        state["opportunities"],
        state["worked_on"],
        by_number,
        stats,
        run_id=state["run_id"],
        brief=state.get("brief"),
    )
    snapshot = {
        "view": "contributor",
        "findings": [{"key": f"opp:#{o['number']}"} for o in state["opportunities"] if is_opportunity(o)],
        "pulse": state["pulse"],
        "opportunities": state["opportunities"],
        "worked_on": state["worked_on"],
        "brief": state.get("brief"),
    }
    path = await save_report(state["repo"], state["repo_id"], state["run_id"], snapshot, markdown, cost)
    return {"report_path": path, "stats": stats}


def _is_contributor(state: HealthState) -> bool:
    return state.get("role") == "contributor"


def _after_link(state: HealthState) -> str:
    return "scout" if _is_contributor(state) else "analyse_groups"


def _after_scout(state: HealthState) -> str:
    return "contributor_report" if _is_contributor(state) else "report"


class NodeFn(Protocol):
    def __call__(self, state: HealthState) -> Awaitable[HealthState]: ...


def _tracked(name: str, fn: NodeFn) -> NodeFn:
    """Record the node's start and end on the run's timeline (Jobs page)."""

    async def node(state: HealthState) -> HealthState:
        await events.record(state.get("run_id"), "node_start", f"{name} started", node=name)
        try:
            out = await fn(state)
        except Exception as e:
            await events.record(state.get("run_id"), "error", f"{type(e).__name__}: {e}", node=name)
            raise
        await events.record(state.get("run_id"), "node_end", f"{name} finished", node=name)
        return out

    return node


def build_graph() -> StateGraph[HealthState]:
    g = StateGraph(HealthState)
    for name, fn in [
        ("fetch", fetch),
        ("link", link),
        ("analyse_groups", analyse_groups),
        ("assess_prs", assess_prs),
        ("verify", verify),
        ("report", report),
        ("scout", scout),
        ("contributor_report", contributor_report),
    ]:
        g.add_node(name, _tracked(name, fn))
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "link")
    g.add_conditional_edges("link", _after_link, ["analyse_groups", "scout"])
    g.add_conditional_edges("scout", _after_scout, ["contributor_report", "report"])
    g.add_edge("contributor_report", END)
    g.add_edge("analyse_groups", "assess_prs")
    g.add_edge("assess_prs", "verify")
    g.add_edge("verify", "scout")
    g.add_edge("report", END)
    return g


def _checkpoint_dsn() -> str:
    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")


async def setup_checkpointer() -> None:
    async with AsyncPostgresSaver.from_conn_string(_checkpoint_dsn()) as saver:
        await saver.setup()


async def run_health_report(repo: str, run_id: str | None = None, resume: bool = False) -> HealthState:
    """Run (or, with `resume=True`, continue) the health report for one repo."""
    run_id = run_id or uuid.uuid4().hex
    config: RunnableConfig = {"configurable": {"thread_id": run_id}}
    async with session_scope() as s:
        repo_id = (await get_or_create_repo(s, repo)).id
    await events.start_run(run_id, repo_id)
    try:
        async with AsyncPostgresSaver.from_conn_string(_checkpoint_dsn()) as saver:
            graph = build_graph().compile(checkpointer=saver)
            initial: HealthState | None = None if resume else {"run_id": run_id, "repo": repo}
            log.info("health_report_start", repo=repo, run_id=run_id, resume=resume)
            result = cast(HealthState, await graph.ainvoke(initial, config))
    except (BudgetExceeded, KillSwitchActive) as e:
        await events.finish_run(run_id, "stopped", error=str(e))
        raise
    except BaseException as e:  # includes cancellation from the web app's Stop button
        await events.finish_run(run_id, "failed", error=f"{type(e).__name__}: {e}")
        raise
    await events.finish_run(run_id, "done", report_path=result.get("report_path"))
    return result
