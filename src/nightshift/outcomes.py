"""What the router learns from: how each model has done at each agent's job.

Two kinds of evidence, per agent and model:
    call  a call that failed: an API error, a refusal, or output the agent could not use. Calls that
          worked are counted from the spend ledger.
    task  the job's real result: the Coder's fix passed verification, the operator approved the plan,
          the Setup agent's environment ran the tests.

`adjust` reorders an agent's ladder from that evidence, and only on clear evidence, because a few
tasks a day make for small samples:
    - a model whose recent calls keep failing for this agent moves to the end;
    - a model whose task success rate beats the first choice's by a wide margin, both over enough
      tasks, moves to the front.
Every move carries its reason, shown on the Jobs page and by `nightshift models`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select

from nightshift.config import model_id
from nightshift.db import LLMCall, ModelOutcome, session_scope

log = structlog.get_logger()

CALL_WINDOW = timedelta(days=14)
TASK_WINDOW = timedelta(days=60)
DEMOTE_FAILURES = 3  # failed calls, and
DEMOTE_RATE = 0.3  # share of recent calls, before a model moves to the end
MIN_TASKS = 5  # task outcomes before a model's success rate is trusted
PROMOTE_MARGIN = 0.2  # how much better a model's success rate must be to take first place
CACHE_SECONDS = 60


@dataclass
class ModelStats:
    calls: int = 0
    failed_calls: int = 0
    tasks: int = 0
    tasks_ok: int = 0
    cost_usd: float = 0.0
    latency_ms: float | None = None

    @property
    def failure_rate(self) -> float:
        return self.failed_calls / self.calls if self.calls else 0.0

    @property
    def success_rate(self) -> float | None:
        return self.tasks_ok / self.tasks if self.tasks else None

    def tasks_text(self) -> str:
        return f"{self.tasks_ok} of {self.tasks}"


async def record(agent: str, model: str, *, ok: bool, kind: str, run_id: str | None = None, detail: str = "") -> None:
    """Save one outcome. Never fails the caller: learning is a bonus, not part of the job."""
    if not model:
        return
    try:
        async with session_scope() as s:
            s.add(ModelOutcome(agent=agent, model=model, run_id=run_id, kind=kind, ok=ok, detail=detail[:1000]))
        _cache.pop(agent, None)
    except Exception as e:  # pragma: no cover - database trouble
        log.warning("outcome_not_recorded", agent=agent, model=model, error=str(e))


_cache: dict[str, tuple[float, dict[str, ModelStats]]] = {}


async def stats(agent: str) -> dict[str, ModelStats]:
    """Per model, for this agent: recent calls and failures, task outcomes, average cost and latency."""
    if (hit := _cache.get(agent)) and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    now = datetime.now(UTC)
    out: dict[str, ModelStats] = {}
    async with session_scope() as s:
        calls = await s.execute(
            select(
                LLMCall.model,
                func.count(),
                func.count(LLMCall.error),
                func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
                func.avg(LLMCall.latency_ms),
            )
            .where(LLMCall.agent == agent, LLMCall.created_at >= now - CALL_WINDOW)
            .group_by(LLMCall.model)
        )
        for model, n, errors, cost, latency in calls.all():
            st = out.setdefault(model_id(model), ModelStats())  # older rows use names like claude-sonnet-5-5
            st.calls += n
            st.failed_calls += errors
            st.cost_usd += float(cost)
            if latency is not None:
                st.latency_ms = float(latency)
        # Refusals and unusable output came back as successful HTTP calls: they are outcomes, not ledger errors.
        bad = await s.execute(
            select(ModelOutcome.model, func.count())
            .where(
                ModelOutcome.agent == agent,
                ModelOutcome.kind == "call",
                ModelOutcome.detail.not_like("api:%"),
                ModelOutcome.created_at >= now - CALL_WINDOW,
            )
            .group_by(ModelOutcome.model)
        )
        for model, n in bad.all():
            out.setdefault(model, ModelStats()).failed_calls += n
        tasks = await s.execute(
            select(ModelOutcome.model, func.count(), func.count().filter(ModelOutcome.ok))
            .where(
                ModelOutcome.agent == agent, ModelOutcome.kind == "task", ModelOutcome.created_at >= now - TASK_WINDOW
            )
            .group_by(ModelOutcome.model)
        )
        for model, n, ok in tasks.all():
            st = out.setdefault(model, ModelStats())
            st.tasks, st.tasks_ok = n, ok
    _cache[agent] = (time.monotonic(), out)
    return out


def adjust(order: list[tuple[str, str]], st: dict[str, ModelStats]) -> list[tuple[str, str]]:
    """Reorder (model, why) pairs from the evidence. Unchanged when the evidence is thin."""
    order = list(order)
    first = order[0][0]
    s0 = st.get(first)
    if s0 and s0.tasks >= MIN_TASKS and s0.success_rate is not None:
        better = [
            (m, why)
            for m, why in order[1:]
            if (sm := st.get(m))
            and sm.tasks >= MIN_TASKS
            and (sm.success_rate or 0) >= s0.success_rate + PROMOTE_MARGIN
        ]
        if better:
            best = max(better, key=lambda p: st[p[0]].success_rate or 0)
            order.remove(best)
            why = f"promoted: {st[best[0]].tasks_text()} tasks succeeded, against {s0.tasks_text()} for {first}"
            order.insert(0, (best[0], why))

    def unreliable(model: str) -> bool:
        sm = st.get(model)
        return bool(sm and sm.failed_calls >= DEMOTE_FAILURES and sm.failure_rate >= DEMOTE_RATE)

    if not all(unreliable(m) for m, _ in order):  # if every model is failing, the order stays
        keep = [(m, why) for m, why in order if not unreliable(m)]
        demoted = [
            (m, f"demoted: {st[m].failed_calls} of {st[m].calls} recent calls failed")
            for m, _ in order
            if unreliable(m)
        ]
        order = keep + demoted
    return order
