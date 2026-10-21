"""The model gateway: the only way any agent reaches Claude.

Every call goes through `LLMGateway.parse`, which
  1. refuses to run when the fleet is killed or a budget cap is reached,
  2. returns a cached verdict when the exact same input was analysed before,
  3. calls Claude with structured output validated against a Pydantic model,
  4. records tokens and cost in the ledger.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import anthropic
import structlog
from anthropic.types import MessageParam, TextBlockParam, ToolParam
from pydantic import BaseModel
from sqlalchemy import func, select

from nightshift import events
from nightshift.config import FALLBACK_MODELS, PRICES, AgentModel, get_settings, openrouter_model
from nightshift.db import AgentVerdict, Control, LLMCall, session_scope

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class KillSwitchActive(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ModelRefused(RuntimeError):
    pass


class AgentOutputError(RuntimeError):
    """The model finished without output matching the schema (e.g. hit max_tokens)."""


def input_hash(*parts: Any) -> str:
    """Stable hash of an agent's inputs. Sorted keys so dict order never busts the cache."""
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def call_cost(model: str, usage: Any) -> float:
    price = PRICES.get(model)
    if price is None:
        return 0.0
    return (
        (usage.input_tokens or 0) * price.input
        + (usage.output_tokens or 0) * price.output
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * price.cache_read
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * price.cache_write
    ) / 1_000_000


def make_client() -> anthropic.AsyncAnthropic:
    settings = get_settings()
    if settings.provider == "openrouter":
        key = settings.openrouter_api_key.get_secret_value()
        if not key:
            raise RuntimeError("NIGHTSHIFT_PROVIDER=openrouter but OPENROUTER_API_KEY is not set.")
        return anthropic.AsyncAnthropic(
            base_url=settings.openrouter_base_url, auth_token=key, api_key=None, max_retries=4
        )
    return anthropic.AsyncAnthropic(max_retries=4)


# Estimated cost of calls that have passed the budget check but not yet been recorded. Without it,
# concurrent calls all see the same "spent so far" and together overshoot a cap.
_in_flight_usd = 0.0
TYPICAL_OUTPUT_TOKENS = 3000


def estimate_cost(cfg: AgentModel, system: str, user: str) -> float:
    price = PRICES.get(cfg.model)
    if price is None:
        return 0.0
    input_tokens = (len(system) + len(user)) / 3  # conservative chars-per-token
    output_tokens = min(cfg.max_tokens, TYPICAL_OUTPUT_TOKENS)
    return (input_tokens * price.input + output_tokens * price.output) / 1_000_000


class LLMGateway:
    def __init__(self, run_id: str, repo_id: int, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.run_id = run_id
        self.repo_id = repo_id
        self.settings = get_settings()
        self.client = client or make_client()
        self._last_cost = 0.0

    async def _check_controls(self, estimate: float) -> None:
        """Raise if the kill switch is on, or if this call (plus calls still in flight) could pass a cap."""
        now = datetime.now(UTC)
        pending = _in_flight_usd + estimate
        async with session_scope() as s:
            control = await s.get(Control, 1)
            if control is not None and control.killed:
                raise KillSwitchActive("Kill switch is on; no model calls allowed.")

            async def spent_since(since: datetime) -> float:
                q = select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.created_at >= since)
                return float((await s.execute(q)).scalar_one())

            day = await spent_since(now.replace(hour=0, minute=0, second=0, microsecond=0))
            month = await spent_since(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
            run_q = select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.run_id == self.run_id)
            run = float((await s.execute(run_q)).scalar_one())

        limits = self.settings
        if day + pending > limits.daily_budget_usd:
            raise BudgetExceeded(f"Daily budget reached (${day:.2f} spent of ${limits.daily_budget_usd:.2f}).")
        if month + pending > limits.monthly_budget_usd:
            raise BudgetExceeded(f"Monthly budget reached (${month:.2f} spent of ${limits.monthly_budget_usd:.2f}).")
        if run + pending > limits.per_report_budget_usd:
            raise BudgetExceeded(f"Report budget reached (${run:.2f} spent of ${limits.per_report_budget_usd:.2f}).")

    async def _cached(self, agent: str, key: str, schema: type[T]) -> T | None:
        async with session_scope() as s:
            row = (
                await s.execute(select(AgentVerdict).where(AgentVerdict.agent == agent, AgentVerdict.input_hash == key))
            ).scalar_one_or_none()
        return schema.model_validate(row.output) if row else None

    async def parse(
        self,
        *,
        agent: str,
        cfg: AgentModel,
        system: str,
        user: str,
        schema: type[T],
        cache_key_parts: tuple[Any, ...],
        label: str = "",
    ) -> T:
        """`label` says what the call is about (e.g. "PR #12") for the Jobs page."""
        key = input_hash(agent, cfg.model, system, user, schema.model_json_schema(), *cache_key_parts)
        if (hit := await self._cached(agent, key, schema)) is not None:
            log.debug("verdict_cache_hit", agent=agent)
            await events.record(self.run_id, "llm_end", f"{agent}: {label} (cached)", agent=agent, cached=True)
            return hit

        global _in_flight_usd
        estimate = estimate_cost(cfg, system, user)
        await self._check_controls(estimate)
        _in_flight_usd += estimate
        await events.record(self.run_id, "llm_start", f"{agent}: {label}", agent=agent, model=cfg.model)
        try:
            result = await self._call(agent=agent, cfg=cfg, system=system, user=user, schema=schema, key=key)
        except Exception as e:
            await events.record(self.run_id, "warning", f"{agent}: {label} failed: {e}", agent=agent)
            raise
        finally:
            _in_flight_usd -= estimate
        await events.record(
            self.run_id, "llm_end", f"{agent}: {label}", agent=agent, model=cfg.model, cost_usd=self._last_cost
        )
        return result

    async def _call(self, *, agent: str, cfg: AgentModel, system: str, user: str, schema: type[T], key: str) -> T:
        kwargs: dict[str, Any] = {}
        model = cfg.model
        if self.settings.provider == "openrouter":
            model = openrouter_model(cfg.model)
        elif cfg.model in FALLBACK_MODELS:
            kwargs = {"betas": [FALLBACK_BETA], "fallbacks": "default"}

        ledger = LLMCall(run_id=self.run_id, agent=agent, model=cfg.model)
        try:
            response = await self.client.beta.messages.parse(
                model=model,
                max_tokens=cfg.max_tokens,
                output_config={"effort": cfg.effort},
                # Only the system prompt repeats across calls; caching the whole (unique) request only adds write cost.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                **kwargs,
            )
        except anthropic.APIError as e:
            ledger.error = f"{type(e).__name__}: {e}"
            async with session_scope() as s:
                s.add(ledger)
            raise

        usage = response.usage
        ledger.request_id = response._request_id
        ledger.stop_reason = response.stop_reason
        ledger.input_tokens = usage.input_tokens or 0
        ledger.output_tokens = usage.output_tokens or 0
        ledger.cache_read_tokens = usage.cache_read_input_tokens or 0
        ledger.cache_write_tokens = usage.cache_creation_input_tokens or 0
        ledger.cost_usd = call_cost(cfg.model, usage)
        self._last_cost = round(ledger.cost_usd, 5)

        async with session_scope() as s:
            s.add(ledger)

        result = response.parsed_output
        if response.stop_reason == "refusal":
            raise ModelRefused(f"{agent} request was declined ({response.stop_details}).")
        if response.stop_reason == "max_tokens" or result is None:
            raise AgentOutputError(f"{agent} returned no parsable output (stop_reason={response.stop_reason}).")
        async with session_scope() as s:
            s.add(
                AgentVerdict(
                    repo_id=self.repo_id,
                    agent=agent,
                    input_hash=key,
                    output=result.model_dump(mode="json"),
                    model=cfg.model,
                )
            )
        log.info(
            "llm_call", agent=agent, model=cfg.model, cost_usd=round(ledger.cost_usd, 5), request_id=ledger.request_id
        )
        return result


class ChatStream:
    """One streamed chat call. Iterate for text deltas; `message` holds the final message afterwards."""

    def __init__(
        self,
        gateway: LLMGateway,
        *,
        agent: str,
        cfg: AgentModel,
        system: list[TextBlockParam],
        messages: list[MessageParam],
        tools: list[ToolParam],
    ) -> None:
        self.gateway, self.agent, self.cfg = gateway, agent, cfg
        self.system, self.messages, self.tools = system, messages, tools
        self.message: Any = None
        self.cost = 0.0

    async def __aiter__(self) -> AsyncIterator[str]:
        global _in_flight_usd
        gw, cfg = self.gateway, self.cfg
        text_len = sum(len(json.dumps(b, default=str)) for b in self.system) + len(
            json.dumps(self.messages, default=str)
        )
        estimate = estimate_cost(cfg, "", "x" * text_len)
        await gw._check_controls(estimate)
        model = openrouter_model(cfg.model) if gw.settings.provider == "openrouter" else cfg.model
        ledger = LLMCall(run_id=gw.run_id, agent=self.agent, model=cfg.model)
        _in_flight_usd += estimate
        try:
            async with gw.client.messages.stream(
                model=model,
                max_tokens=cfg.max_tokens,
                output_config={"effort": cfg.effort},
                system=self.system,
                messages=self.messages,
                tools=self.tools,
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield event.text
                self.message = await stream.get_final_message()
        except anthropic.APIError as e:
            ledger.error = f"{type(e).__name__}: {e}"
            async with session_scope() as s:
                s.add(ledger)
            raise
        finally:
            _in_flight_usd -= estimate
        usage = self.message.usage
        ledger.request_id = getattr(self.message, "_request_id", None)
        ledger.stop_reason = self.message.stop_reason
        ledger.input_tokens = usage.input_tokens or 0
        ledger.output_tokens = usage.output_tokens or 0
        ledger.cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        ledger.cache_write_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
        ledger.cost_usd = self.cost = call_cost(cfg.model, usage)
        async with session_scope() as s:
            s.add(ledger)


async def spend_summary() -> dict[str, float]:
    now = datetime.now(UTC)
    async with session_scope() as s:

        async def since(t: datetime) -> float:
            q = select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(LLMCall.created_at >= t)
            return float((await s.execute(q)).scalar_one())

        return {
            "today": await since(now.replace(hour=0, minute=0, second=0, microsecond=0)),
            "last_7_days": await since(now - timedelta(days=7)),
            "this_month": await since(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
        }
