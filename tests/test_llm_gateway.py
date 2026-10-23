"""Gateway tests against the local Postgres (docker compose up). Skipped when it is not running."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import delete

from nightshift import db, llm
from nightshift.config import AgentModel
from nightshift.db import AgentVerdict, Control, LLMCall, Repo, session_scope
from nightshift.llm import BudgetExceeded, KillSwitchActive, LLMGateway, ModelRefused


class Answer(BaseModel):
    value: str


class FakeMessages:
    def __init__(self, stop_reason: str = "end_turn") -> None:
        self.calls = 0
        self.stop_reason = stop_reason

    async def parse(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.last_kwargs = kwargs
        return SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0
            ),
            stop_reason=self.stop_reason,
            stop_details=None,
            parsed_output=Answer(value="ok") if self.stop_reason == "end_turn" else None,
            _request_id="req_test",
        )


def fake_client(messages: FakeMessages) -> Any:
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


CFG = AgentModel(model="claude-opus-5-5", effort="low")


@pytest.fixture
async def repo_id() -> AsyncIterator[int]:
    try:
        async with session_scope() as s:
            repo = Repo(full_name=f"test/{uuid.uuid4().hex[:8]}")
            s.add(repo)
            await s.flush()
            rid = repo.id
    except Exception as e:  # pragma: no cover - environment dependent
        await db.get_engine().dispose()
        db._engine = None
        pytest.skip(f"Postgres not available: {e}")
    yield rid
    async with session_scope() as s:
        await s.execute(delete(LLMCall).where(LLMCall.agent == "test_agent"))
        await s.execute(delete(AgentVerdict).where(AgentVerdict.repo_id == rid))
        await s.execute(delete(Repo).where(Repo.id == rid))
        await s.execute(delete(Control))
    await db.get_engine().dispose()
    db._engine = None


def _gateway(repo_id: int, msgs: FakeMessages) -> LLMGateway:
    """A gateway whose day/month caps ignore real spend already in the shared dev database."""
    gw = LLMGateway(uuid.uuid4().hex, repo_id, client=fake_client(msgs))
    gw.settings = gw.settings.model_copy(update={"daily_budget_usd": 1e9, "monthly_budget_usd": 1e9})
    return gw


async def _ask(gw: LLMGateway, user: str = "q") -> Answer:
    return await gw.parse(agent="test_agent", cfg=CFG, system="s", user=user, schema=Answer, cache_key_parts=())


async def test_records_cost_and_caches_verdict(repo_id: int) -> None:
    msgs = FakeMessages()
    gw = _gateway(repo_id, msgs)
    gw.settings = gw.settings.model_copy(update={"provider": "anthropic"})
    assert (await _ask(gw, "unique-" + uuid.uuid4().hex)).value == "ok"
    assert msgs.last_kwargs["fallbacks"] == "default"
    assert msgs.last_kwargs["output_config"] == {"effort": "low"}

    user = "repeat-" + uuid.uuid4().hex
    await _ask(gw, user)
    await _ask(gw, user)
    assert msgs.calls == 2  # third call served from the verdict cache


async def test_openrouter_uses_its_model_names(repo_id: int) -> None:
    msgs = FakeMessages()
    gw = _gateway(repo_id, msgs)
    gw.settings = gw.settings.model_copy(update={"provider": "openrouter"})
    await _ask(gw, "unique-" + uuid.uuid4().hex)
    assert msgs.last_kwargs["model"] == "anthropic/claude-opus-5.5"
    assert "fallbacks" not in msgs.last_kwargs


async def test_kill_switch_blocks_calls(repo_id: int) -> None:
    async with session_scope() as s:
        s.add(Control(id=1, killed=True))
    msgs = FakeMessages()
    gw = _gateway(repo_id, msgs)
    with pytest.raises(KillSwitchActive):
        await _ask(gw, uuid.uuid4().hex)
    assert msgs.calls == 0


async def test_run_budget_blocks_calls(repo_id: int) -> None:
    msgs = FakeMessages()
    gw = _gateway(repo_id, msgs)
    gw.run_budget_usd = 0.001
    with pytest.raises(BudgetExceeded):  # the call's own estimate already exceeds the cap
        await _ask(gw, uuid.uuid4().hex)
    assert msgs.calls == 0


async def test_in_flight_calls_count_against_budget(repo_id: int) -> None:
    """Concurrent calls must not all pass the check on the same 'spent so far' figure."""
    gate = asyncio.Event()

    class SlowMessages(FakeMessages):
        async def parse(self, **kwargs: Any) -> Any:
            await gate.wait()
            return await super().parse(**kwargs)

    msgs = SlowMessages()
    gw = _gateway(repo_id, msgs)
    one_call = llm.estimate_cost(CFG, "s", "x" * 32)
    gw.run_budget_usd = one_call * 1.5
    first = asyncio.create_task(_ask(gw, "x" * 32))
    await asyncio.sleep(0.05)  # first call is now in flight, not yet recorded
    with pytest.raises(BudgetExceeded):
        await _ask(gw, "y" * 32)
    gate.set()
    await first


async def test_refusal_is_recorded_not_cached(repo_id: int) -> None:
    msgs = FakeMessages(stop_reason="refusal")
    gw = _gateway(repo_id, msgs)
    user = uuid.uuid4().hex
    with pytest.raises(ModelRefused):
        await _ask(gw, user)
    with pytest.raises(ModelRefused):
        await _ask(gw, user)
    assert msgs.calls == 2
