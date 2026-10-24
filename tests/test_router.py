"""The router's choices, and the gateway moving along a route when a model fails."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fake_openrouter import FakeOpenRouter, reply
from pydantic import BaseModel

from nightshift import llm
from nightshift.catalog import ModelConfigError
from nightshift.config import AgentModel
from nightshift.llm import LLMGateway, ModelAPIError
from nightshift.router import RouteHint, family, routes

SONNET, OPUS, SOL = "anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5", "openai/gpt-5.6-sol"
CODER = AgentModel(model=SONNET, ladder=[OPUS])
REVIEWER = AgentModel(model=SOL, ladder=[SONNET, OPUS])


async def _ids(cfg: AgentModel, hint: RouteHint | None = None, **kw: bool) -> list[str]:
    return [r.info.id for r in await routes(cfg, hint, **kw)]


async def test_first_choice_then_up_the_ladder(models: Any) -> None:
    assert await _ids(CODER) == [SONNET, OPUS]


async def test_escalation_starts_higher_and_falls_back_down(models: Any) -> None:
    rs = await routes(CODER, RouteHint(level=1))
    assert [r.info.id for r in rs] == [OPUS, SONNET]
    assert rs[0].why == "escalated to ladder step 1"
    assert await _ids(CODER, RouteHint(level=9)) == [OPUS, SONNET]  # past the top: the top


async def test_reviewer_puts_the_coders_family_last(models: Any) -> None:
    assert await _ids(REVIEWER, RouteHint(avoid="openai")) == [SONNET, OPUS, SOL]
    assert await _ids(REVIEWER, RouteHint(avoid="anthropic")) == [SOL, SONNET, OPUS]
    assert family(SONNET) == "anthropic"


async def test_models_that_cannot_do_the_job_are_skipped(models: Any) -> None:
    cfg = AgentModel(model="old/no-tools", ladder=["gone/model", SONNET])
    assert await _ids(cfg, tools=True) == [SONNET]
    with pytest.raises(ModelConfigError, match="cannot call tools.*not on OpenRouter"):
        await routes(AgentModel(model="old/no-tools", ladder=["gone/model"]), tools=True)


# ---------- fallback in the gateway (no database: the ledger and events are stubbed) ----------


class Answer(BaseModel):
    value: str


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch, models: Any) -> Any:
    async def nothing(*_: Any, **__: Any) -> Any:
        return None

    monkeypatch.setattr(LLMGateway, "_check_controls", nothing)
    monkeypatch.setattr(LLMGateway, "_cached", nothing)

    async def record(ledger: Any, *_: Any) -> None:
        ledger.cost_usd = 0.0

    monkeypatch.setattr(llm, "_record", record)
    monkeypatch.setattr(llm.events, "record", nothing)

    class _Session:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        def add(self, _: Any) -> None:
            return None

    monkeypatch.setattr(llm, "session_scope", _Session)

    def make(client: Any) -> LLMGateway:
        return LLMGateway(uuid.uuid4().hex, 1, client=client)

    return make


class Failing(FakeOpenRouter):
    """Fails every request for the models in `bad`."""

    def __init__(self, bad: dict[str, Exception], *replies: dict[str, Any]) -> None:
        super().__init__(*replies)
        self.bad = bad

    async def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        if body["model"] in self.bad:
            self.bodies.append(body)
            raise self.bad[body["model"]]
        return await super().complete(body)


async def test_parse_moves_to_the_next_model_when_one_fails(gateway: Any) -> None:
    fake = Failing({SONNET: ModelAPIError(503, "overloaded")}, reply('{"value": "ok"}'))
    out = await gateway(fake).parse(agent="t", cfg=CODER, system="s", user="u", schema=Answer, cache_key_parts=())
    assert out.value == "ok"
    assert [b["model"] for b in fake.bodies] == [SONNET, OPUS]


async def test_parse_moves_on_after_unusable_output(gateway: Any) -> None:
    fake = FakeOpenRouter(reply("nope"), reply("still nope"), reply('{"value": "ok"}'))
    out = await gateway(fake).parse(agent="t", cfg=CODER, system="s", user="u", schema=Answer, cache_key_parts=())
    assert out.value == "ok" and [b["model"] for b in fake.bodies] == [SONNET, SONNET, OPUS]


async def test_out_of_credits_is_not_a_model_problem(gateway: Any) -> None:
    fake = Failing({SONNET: ModelAPIError(402, "Insufficient credits")}, reply('{"value": "ok"}'))
    with pytest.raises(ModelAPIError, match="402"):
        await gateway(fake).parse(agent="t", cfg=CODER, system="s", user="u", schema=Answer, cache_key_parts=())
    assert [b["model"] for b in fake.bodies] == [SONNET]


async def test_a_refused_tool_turn_goes_to_the_next_model(gateway: Any) -> None:
    fake = FakeOpenRouter(
        reply(None, refusal="no"), reply("Fine.", tool_calls=[("c1", "run", {})], finish="tool_calls")
    )
    turn = await gateway(fake).create(agent="coder", cfg=CODER, system=[], messages=[{"role": "user", "content": "go"}],
                                      tools=[{"name": "run", "input_schema": {"type": "object"}}])  # fmt: skip
    assert turn.model == OPUS and turn.stop_reason == "tool_use"


def test_reasoning_goes_only_to_the_model_that_wrote_it() -> None:
    messages = [{"role": "assistant", "content": [
        {"type": "reasoning", "model": SONNET, "details": [{"type": "reasoning.text", "text": "hm"}]},
        {"type": "text", "text": "Done."},
    ]}]  # fmt: skip
    assert "reasoning_details" in llm.to_wire([], messages, SONNET)[0]
    assert "reasoning_details" not in llm.to_wire([], messages, SOL)[0]
