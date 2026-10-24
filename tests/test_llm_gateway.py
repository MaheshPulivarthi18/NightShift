"""Gateway tests. The ones that touch the ledger use the local Postgres (docker compose up) and are
skipped when it is not running; the translation tests need nothing."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fake_openrouter import FakeOpenRouter, reply
from pydantic import BaseModel
from sqlalchemy import delete, select

from nightshift import db, llm
from nightshift.catalog import ModelConfigError, ModelInfo
from nightshift.config import AgentModel
from nightshift.db import AgentVerdict, Control, LLMCall, Repo, session_scope
from nightshift.llm import (
    INVALID_ARGS,
    AgentOutputError,
    BudgetExceeded,
    KillSwitchActive,
    LLMGateway,
    ModelAPIError,
    ModelRefused,
    OpenRouter,
)


class Answer(BaseModel):
    value: str


CFG = AgentModel(model="claude-opus-5-5", effort="low")
GOOD = reply('{"value": "ok"}')


@pytest.fixture
async def repo_id(models: Any) -> AsyncIterator[int]:
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


def _gateway(repo_id: int, client: Any) -> LLMGateway:
    """A gateway whose day/month caps ignore real spend already in the shared dev database."""
    gw = LLMGateway(uuid.uuid4().hex, repo_id, client=client)
    gw.settings = gw.settings.model_copy(update={"daily_budget_usd": 1e9, "monthly_budget_usd": 1e9})
    return gw


async def _ask(gw: LLMGateway, user: str = "q", cfg: AgentModel = CFG) -> Answer:
    return await gw.parse(agent="test_agent", cfg=cfg, system="s", user=user, schema=Answer, cache_key_parts=())


# ---------- parse ----------


async def test_records_billed_cost_and_caches_verdict(repo_id: int) -> None:
    fake = FakeOpenRouter(reply('{"value": "ok"}', cost=0.0123))
    gw = _gateway(repo_id, fake)
    assert (await _ask(gw, "unique-" + uuid.uuid4().hex)).value == "ok"
    body = fake.bodies[0]
    assert body["model"] == "anthropic/claude-opus-5.5"
    assert body["reasoning"] == {"effort": "low"}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert gw.last_cost == 0.0123
    async with session_scope() as s:
        row = (await s.execute(select(LLMCall).where(LLMCall.run_id == gw.run_id))).scalar_one()
    assert (row.cost_usd, row.provider, row.model) == (0.0123, "TestCloud", "anthropic/claude-opus-5.5")
    assert row.latency_ms is not None

    user = "repeat-" + uuid.uuid4().hex
    await _ask(gw, user)
    await _ask(gw, user)
    assert len(fake.bodies) == 2  # the third call is served from the verdict cache


async def test_any_vendor_and_no_reasoning_setting(repo_id: int) -> None:
    fake = FakeOpenRouter(GOOD)
    gw = _gateway(repo_id, fake)
    await _ask(gw, uuid.uuid4().hex, AgentModel(model="deepseek/deepseek-v4.1-flash", effort="high"))
    assert fake.bodies[0]["model"] == "deepseek/deepseek-v4.1-flash"
    assert fake.bodies[0]["reasoning"] == {"effort": "high"}  # no listed efforts: sent as asked


async def test_invalid_json_gets_one_repair(repo_id: int) -> None:
    fake = FakeOpenRouter(reply('```json\n{"wrong": 1}\n```'), GOOD)
    gw = _gateway(repo_id, fake)
    assert (await _ask(gw, uuid.uuid4().hex)).value == "ok"
    assert len(fake.bodies) == 2
    assert "did not match the schema" in fake.bodies[1]["messages"][-1]["content"]

    fake = FakeOpenRouter(reply("not json"))
    with pytest.raises(AgentOutputError):
        await _ask(_gateway(repo_id, fake), uuid.uuid4().hex)
    assert len(fake.bodies) == 2


async def test_cut_off_output_is_an_agent_error(repo_id: int) -> None:
    with pytest.raises(AgentOutputError, match="cut off"):
        await _ask(_gateway(repo_id, FakeOpenRouter(reply('{"val', finish="length"))), uuid.uuid4().hex)


async def test_a_model_that_cannot_do_the_job_is_refused_before_calling(repo_id: int) -> None:
    fake = FakeOpenRouter(GOOD)
    with pytest.raises(ModelConfigError, match="not on OpenRouter"):
        await _ask(_gateway(repo_id, fake), "q", AgentModel(model="nobody/nothing"))
    with pytest.raises(ModelConfigError, match="structured output"):
        await _ask(_gateway(repo_id, fake), "q", AgentModel(model="old/no-tools"))
    assert fake.bodies == []


async def test_kill_switch_blocks_calls(repo_id: int) -> None:
    async with session_scope() as s:
        s.add(Control(id=1, killed=True))
    fake = FakeOpenRouter(GOOD)
    with pytest.raises(KillSwitchActive):
        await _ask(_gateway(repo_id, fake), uuid.uuid4().hex)
    assert fake.bodies == []


async def test_run_budget_blocks_calls(repo_id: int) -> None:
    fake = FakeOpenRouter(GOOD)
    gw = _gateway(repo_id, fake)
    gw.run_budget_usd = 0.001
    with pytest.raises(BudgetExceeded):  # the call's own estimate already exceeds the cap
        await _ask(gw, uuid.uuid4().hex)
    assert fake.bodies == []


async def test_in_flight_calls_count_against_budget(repo_id: int, models: list[ModelInfo]) -> None:
    """Concurrent calls must not all pass the check on the same 'spent so far' figure."""
    gate = asyncio.Event()

    class Slow(FakeOpenRouter):
        async def complete(self, body: dict[str, Any]) -> dict[str, Any]:
            await gate.wait()
            return await super().complete(body)

    gw = _gateway(repo_id, Slow(GOOD))
    one_call = llm.estimate_cost(models[0], CFG, len("s") + 32)
    gw.run_budget_usd = one_call * 1.5
    first = asyncio.create_task(_ask(gw, "x" * 32))
    await asyncio.sleep(0.05)  # the first call is now in flight, not yet recorded
    with pytest.raises(BudgetExceeded):
        await _ask(gw, "y" * 32)
    gate.set()
    await first


async def test_refusal_is_recorded_not_cached(repo_id: int) -> None:
    fake = FakeOpenRouter(reply(None, refusal="no"))
    gw = _gateway(repo_id, fake)
    user = uuid.uuid4().hex
    for _ in range(2):
        with pytest.raises(ModelRefused):
            await _ask(gw, user)
    assert len(fake.bodies) == 2


# ---------- tool turns ----------

TOOLS = [{"name": "read_file", "description": "Read", "input_schema": {"type": "object", "properties": {}}}]


async def test_create_returns_tool_calls_in_the_block_format(repo_id: int) -> None:
    calls = [("c1", "read_file", {"path": "a.py"}), ("c2", "read_file", "{oops")]
    fake = FakeOpenRouter(reply("Reading.", tool_calls=calls, finish="tool_calls"))
    gw = _gateway(repo_id, fake)
    turn = await gw.create(agent="test_agent", cfg=CFG, system=[{"type": "text", "text": "sys"}],
                           messages=[{"role": "user", "content": "go"}], tools=TOOLS)  # fmt: skip
    assert turn.stop_reason == "tool_use" and turn.provider == "TestCloud"
    assert [(b.type, b.input) for b in turn.content] == [
        ("text", None), ("tool_use", {"path": "a.py"}), ("tool_use", {INVALID_ARGS: "{oops"}),
    ]  # fmt: skip
    assert fake.bodies[0]["tools"][0] == {
        "type": "function",
        "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {}}},
    }
    with pytest.raises(ModelConfigError, match="cannot call tools"):
        await gw.create(agent="test_agent", cfg=AgentModel(model="old/no-tools"), system=[], messages=[], tools=TOOLS)


def test_conversation_translates_to_openrouter_messages() -> None:
    messages = [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": [
            {"type": "reasoning", "details": [{"type": "reasoning.encrypted", "data": "x"}]},
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "a.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "no such file", "is_error": True,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Also check b.py."},
        ]},
    ]  # fmt: skip
    rules = {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}
    wire = llm.to_wire([rules], messages)
    assert wire[0] == {"role": "system", "content": [rules]}
    assert wire[2]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "a.py"}'}
    assert wire[2]["reasoning_details"] == [{"type": "reasoning.encrypted", "data": "x"}]
    assert wire[3] == {
        "role": "tool", "tool_call_id": "c1",
        "content": [{"type": "text", "text": "Error: no such file", "cache_control": {"type": "ephemeral"}}],
    }  # fmt: skip
    assert wire[4] == {"role": "user", "content": [{"type": "text", "text": "Also check b.py."}]}


def test_strict_schema_requires_every_field_and_keeps_field_names() -> None:
    class Inner(BaseModel):
        default: str = "x"  # a field that happens to be called "default"

    class Outer(BaseModel):
        title: str
        inner: Inner
        items: list[Inner] = []

    shape = llm.strict_schema(Outer.model_json_schema())
    assert shape["required"] == ["title", "inner", "items"] and shape["additionalProperties"] is False
    inner = shape["$defs"]["Inner"]
    assert inner["required"] == ["default"] and "default" not in inner["properties"]["default"]


def test_streamed_reasoning_pieces_are_joined() -> None:
    parts = [
        {"type": "reasoning.text", "text": "Let me ", "index": 0, "format": "anthropic-claude-v1"},
        {"type": "reasoning.text", "text": "look.", "index": 0, "signature": "sig"},
    ]
    (joined,) = llm._merge_reasoning(parts)
    assert joined == {**parts[0], "text": "Let me look.", "signature": "sig"}


def test_effort_falls_back_to_the_nearest_the_model_accepts() -> None:
    m = ModelInfo(id="x/y", supported=["reasoning"], efforts=["low", "high"])
    assert (m.effort("low"), m.effort("medium"), m.effort("max")) == ("low", "low", "high")
    assert ModelInfo(id="x/z", supported=["tools"]).effort("high") is None


# ---------- HTTP ----------


async def test_client_retries_busy_answers_and_reports_credit_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(llm.asyncio, "sleep", no_wait)
    answers = iter([
        httpx.Response(429, json={"error": {"code": 429, "message": "slow down"}}),
        httpx.Response(200, json={"error": {"code": 502, "message": "provider hiccup"}}),
        httpx.Response(200, json=GOOD),
        httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}}),
    ])  # fmt: skip
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(answers)))
    client = OpenRouter(http)
    assert (await client.complete({"model": "m"}))["id"] == "gen-test"
    with pytest.raises(ModelAPIError) as e:
        await client.complete({"model": "m"})
    assert (e.value.status, e.value.message) == (402, "Insufficient credits")
