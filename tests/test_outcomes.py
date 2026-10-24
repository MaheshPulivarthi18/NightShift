"""What the router learns: reordering a ladder from outcomes, and the stats it reads them from."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import delete

from nightshift import outcomes
from nightshift.config import AgentModel
from nightshift.db import LLMCall, ModelOutcome, session_scope
from nightshift.outcomes import ModelStats, adjust
from nightshift.router import routes

A, B, C = "anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5", "openai/gpt-5.6-sol"
ORDER = [(A, "first choice"), (B, "ladder step 1"), (C, "ladder step 2")]


def test_thin_evidence_changes_nothing() -> None:
    assert adjust(ORDER, {}) == ORDER
    few = {A: ModelStats(tasks=2, tasks_ok=0), B: ModelStats(tasks=2, tasks_ok=2)}  # under MIN_TASKS
    assert adjust(ORDER, few) == ORDER
    two_failures = {A: ModelStats(calls=4, failed_calls=2)}  # under DEMOTE_FAILURES
    assert adjust(ORDER, two_failures) == ORDER


def test_a_clearly_better_model_takes_first_place() -> None:
    st = {A: ModelStats(tasks=6, tasks_ok=2), B: ModelStats(tasks=5, tasks_ok=4), C: ModelStats(tasks=5, tasks_ok=5)}
    out = adjust(ORDER, st)
    assert [m for m, _ in out] == [C, A, B]
    assert out[0][1] == f"promoted: 5 of 5 tasks succeeded, against 2 of 6 for {A}"


def test_a_slightly_better_model_does_not() -> None:
    st = {A: ModelStats(tasks=10, tasks_ok=7), B: ModelStats(tasks=10, tasks_ok=8)}  # +0.1 < PROMOTE_MARGIN
    assert adjust(ORDER, st) == ORDER


def test_a_model_whose_calls_keep_failing_goes_last() -> None:
    st = {A: ModelStats(calls=10, failed_calls=4)}
    out = adjust(ORDER, st)
    assert [m for m, _ in out] == [B, C, A]
    assert out[-1][1] == "demoted: 4 of 10 recent calls failed"


def test_when_every_model_fails_the_order_stays() -> None:
    st = {m: ModelStats(calls=5, failed_calls=5) for m, _ in ORDER}
    assert adjust(ORDER, st) == ORDER


async def test_the_router_applies_what_it_learned(models: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def learned(agent: str) -> dict[str, ModelStats]:
        return {A: ModelStats(calls=10, failed_calls=5)} if agent == "coder" else {}

    monkeypatch.setattr(outcomes, "stats", learned)
    cfg = AgentModel(model=A, ladder=[B])
    assert [r.info.id for r in await routes(cfg, agent="coder")] == [B, A]
    assert [r.info.id for r in await routes(cfg, agent="planner")] == [A, B]
    assert [r.info.id for r in await routes(cfg)] == [A, B]  # no agent: config order


@pytest.mark.real_outcomes
async def test_stats_combine_the_ledger_and_outcomes() -> None:
    agent = f"test-{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            s.add_all(
                [
                    LLMCall(agent=agent, model="claude-sonnet-5-5", cost_usd=0.5, latency_ms=1000),  # an old-style name
                    LLMCall(agent=agent, model=A, cost_usd=0.25, latency_ms=3000),
                    LLMCall(agent=agent, model=A, error="503: overloaded"),
                ]
            )
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    try:
        await outcomes.record(agent, A, ok=False, kind="call", detail="api: 503: overloaded")  # also in the ledger
        await outcomes.record(agent, A, ok=False, kind="call", detail="AgentOutputError: cut off")
        await outcomes.record(agent, A, ok=True, kind="task", detail="fix verified")
        await outcomes.record(agent, A, ok=False, kind="task", detail="verification did not pass")
        st = (await outcomes.stats(agent))[A]
        assert (st.calls, st.failed_calls) == (3, 2)  # the API error counted once
        assert (st.tasks, st.tasks_ok) == (2, 1)
        assert st.cost_usd == 0.75
    finally:
        async with session_scope() as s:
            await s.execute(delete(LLMCall).where(LLMCall.agent == agent))
            await s.execute(delete(ModelOutcome).where(ModelOutcome.agent == agent))
