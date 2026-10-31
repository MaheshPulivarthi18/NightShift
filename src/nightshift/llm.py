"""The model gateway: the only way any agent reaches a model.

Every call goes through OpenRouter's chat completions API, whatever the model's vendor (Anthropic,
OpenAI, Google, DeepSeek...). Inside NightShift a conversation keeps one block format: text,
tool_use, tool_result and reasoning blocks, the shape saved chats already use. It is translated to
OpenRouter's messages here and nowhere else.

Every call
  1. refuses to run when the fleet is killed or a budget cap is reached,
  2. (`parse`) returns a cached verdict when the exact same input was analysed before,
  3. checks in the catalog that the model can do the job (tool calls, structured output),
  4. records tokens, the cost OpenRouter billed, the provider it chose and the latency in the ledger.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import httpx
import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from nightshift import events, outcomes
from nightshift.catalog import ModelConfigError, ModelInfo
from nightshift.config import AgentModel, get_settings
from nightshift.db import AgentVerdict, Control, LLMCall, session_scope
from nightshift.prompt import Prompt
from nightshift.router import Route, RouteHint, routes

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

# A conversation message in NightShift's block format: {"role": "user"|"assistant", "content": str | [block]}.
MessageParam = dict[str, Any]
# A tool: {"name", "description", "input_schema"}.
ToolParam = dict[str, Any]
TextBlockParam = dict[str, Any]

INVALID_ARGS = "__invalid_json__"  # a tool_use whose arguments were not a JSON object
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
STOP_REASONS = {"tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}


class KillSwitchActive(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ModelRefused(RuntimeError):
    pass


class AgentOutputError(RuntimeError):
    """The model finished without output matching the schema (e.g. cut off)."""


class ModelAPIError(RuntimeError):
    """OpenRouter or the model provider returned an error. `status` 401: bad key, 402: out of credits."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


# ---------- the block format ----------


class Block(BaseModel):
    """One piece of a model turn: text, a tool call, or reasoning to send back with the next request."""

    type: str  # text | tool_use | reasoning
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    details: list[dict[str, Any]] | None = None  # reasoning: OpenRouter's reasoning_details, sent back as-is
    model: str | None = None  # reasoning: the model that wrote it; another model is not sent it


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class Turn(BaseModel):
    """A model's answer. stop_reason: end_turn, tool_use, max_tokens or refusal."""

    content: list[Block]
    stop_reason: str
    usage: Usage = Usage()
    model: str = ""
    provider: str | None = None


def _text_parts(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": b["text"], **({"cache_control": b["cache_control"]} if b.get("cache_control") else {})}
        for b in blocks
        if b.get("type") == "text" and b.get("text")
    ]


def _tool_message(block: dict[str, Any]) -> dict[str, Any]:
    text = block.get("content", "")
    if not isinstance(text, str):
        text = "\n".join(str(p.get("text", p)) if isinstance(p, dict) else str(p) for p in text)
    if block.get("is_error"):
        text = f"Error: {text}"
    content: Any = text
    if block.get("cache_control"):
        content = [{"type": "text", "text": text, "cache_control": block["cache_control"]}]
    return {"role": "tool", "tool_call_id": block["tool_use_id"], "content": content}


def to_wire(
    system: str | list[TextBlockParam], messages: list[MessageParam], model: str | None = None
) -> list[dict[str, Any]]:
    """NightShift's block-format conversation as OpenRouter chat messages, for `model`: reasoning that
    another model wrote (before the router switched models) is left out."""
    sys_parts = [{"type": "text", "text": system}] if isinstance(system, str) else _text_parts(system)
    out: list[dict[str, Any]] = [{"role": "system", "content": sys_parts}] if sys_parts else []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
        elif m["role"] == "assistant":
            text = "".join(b.get("text") or "" for b in content if b.get("type") == "text")
            calls = [
                {"id": b["id"], "type": "function",
                 "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                for b in content if b.get("type") == "tool_use"
            ]  # fmt: skip
            msg: dict[str, Any] = {"role": "assistant", "content": text or (None if calls else "")}
            if calls:
                msg["tool_calls"] = calls
            details = [
                d
                for b in content
                if b.get("type") == "reasoning" and (model is None or b.get("model") in (None, model))
                for d in b.get("details") or []
            ]
            if details:
                msg["reasoning_details"] = details
            out.append(msg)
        else:  # tool results must directly follow the assistant's tool calls, then any user text
            out += [_tool_message(b) for b in content if b.get("type") == "tool_result"]
            if parts := _text_parts(content):
                out.append({"role": "user", "content": parts})
    return out


def to_wire_tools(tools: list[ToolParam]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                          "parameters": t["input_schema"]}}
        for t in tools
    ]  # fmt: skip


def _arguments(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {INVALID_ARGS: raw or ""}
    return value if isinstance(value, dict) else {INVALID_ARGS: raw or ""}


def to_turn(message: dict[str, Any], finish_reason: str | None, usage: dict[str, Any], model: str) -> Turn:
    """An OpenRouter assistant message as a Turn in the block format."""
    blocks: list[Block] = []
    if message.get("reasoning_details"):
        blocks.append(Block(type="reasoning", details=message["reasoning_details"], model=model))
    if message.get("content"):
        blocks.append(Block(type="text", text=message["content"]))
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        blocks.append(
            Block(type="tool_use", id=call.get("id"), name=fn.get("name"), input=_arguments(fn.get("arguments")))
        )
    stop = STOP_REASONS.get(finish_reason or "", "end_turn")
    if message.get("refusal"):
        stop = "refusal"
    elif stop == "end_turn" and any(b.type == "tool_use" for b in blocks):
        stop = "tool_use"
    return Turn(
        content=blocks,
        stop_reason=stop,
        usage=Usage(input_tokens=usage.get("prompt_tokens") or 0, output_tokens=usage.get("completion_tokens") or 0),
        model=model,
    )


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A Pydantic JSON schema in the strict form structured output needs: every property required,
    no extra properties, no defaults."""
    out = copy.deepcopy(schema)

    def fix(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                fix(item)
            return
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        if isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
            for prop in node["properties"].values():
                fix(prop)
        for key in ("items", "anyOf", "allOf", "oneOf", "prefixItems"):
            fix(node.get(key))
        for defs in ("$defs", "definitions"):
            for sub in (node.get(defs) or {}).values():
                fix(sub)

    fix(out)
    return out


# ---------- OpenRouter ----------


class OpenRouter:
    """The HTTP client. Tests pass a fake with the same `complete` and `stream` methods."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        key = settings.openrouter_api_key.get_secret_value()
        if not key and http is None:
            raise ModelConfigError("OPENROUTER_API_KEY is not set.")
        self.url = settings.openrouter_base_url.rstrip("/") + "/chat/completions"
        self.deadline = settings.model_call_seconds
        self.http = http or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}", "X-Title": "NightShift"}, timeout=httpx.Timeout(600, connect=30)
        )

    @staticmethod
    def _error(status: int, body: Any) -> ModelAPIError:
        err = body.get("error") if isinstance(body, dict) else None
        message = (err or {}).get("message") if isinstance(err, dict) else str(body)[:500]
        return ModelAPIError(int((err or {}).get("code") or status) if isinstance(err, dict) else status, message or "")

    async def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(5):
            try:
                res = await asyncio.wait_for(self.http.post(self.url, json=body), self.deadline)
            except TimeoutError as e:  # no retry on the same model: the gateway falls back to the next one
                raise ModelAPIError(504, f"no answer within {self.deadline} s") from e
            except httpx.TransportError as e:
                if attempt == 4:
                    raise ModelAPIError(0, f"cannot reach OpenRouter: {e}") from e
            else:
                data = res.json() if res.content else {}
                if res.status_code == 200 and "error" not in data:
                    return dict(data)
                status = res.status_code if res.status_code != 200 else 502
                if status not in RETRY_STATUS or attempt == 4:
                    raise self._error(status, data)
            await asyncio.sleep(min(2**attempt, 20))
        raise AssertionError("unreachable")

    async def stream(self, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        started = time.monotonic()
        async with self.http.stream("POST", self.url, json={**body, "stream": True}) as res:
            if res.status_code != 200:
                raw = await res.aread()
                try:
                    data: Any = json.loads(raw)
                except ValueError:
                    data = raw.decode(errors="replace")
                raise self._error(res.status_code, data)
            async for line in res.aiter_lines():
                if time.monotonic() - started > self.deadline:  # kept alive but never finishing
                    raise ModelAPIError(504, f"no complete answer within {self.deadline} s")
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue  # comments like ": OPENROUTER PROCESSING" keep the connection alive
                chunk = json.loads(line[6:])
                if "error" in chunk:
                    raise self._error(502, chunk)
                yield chunk


def make_client() -> OpenRouter:
    return OpenRouter()


# ---------- gateway ----------


def input_hash(*parts: Any) -> str:
    """Stable hash of an agent's inputs. Sorted keys so dict order never busts the cache."""
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# Estimated cost of calls that have passed the budget check but not yet been recorded. Without it,
# concurrent calls all see the same "spent so far" and together overshoot a cap.
_in_flight_usd = 0.0
TYPICAL_OUTPUT_TOKENS = 3000
# What the current parse/create call has spent so far, across repairs and fallbacks. Per asyncio
# context, so concurrent calls on one gateway (the scout runs several at once) never mix their costs.
_spent: ContextVar[list[float] | None] = ContextVar("_spent", default=None)
# The layer report of the prompt the current parse call sends (prompt.Prompt.report), kept in the ledger.
_layers: ContextVar[dict[str, Any] | None] = ContextVar("_layers", default=None)


def estimate_cost(info: ModelInfo, cfg: AgentModel, chars: int) -> float:
    return info.cost(chars / 3, min(cfg.max_tokens, TYPICAL_OUTPUT_TOKENS))  # conservative chars-per-token


def _request(info: ModelInfo, cfg: AgentModel, messages: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": info.id,
        "messages": messages,
        "max_tokens": min(cfg.max_tokens, info.max_output or cfg.max_tokens),
        "usage": {"include": True},
    }
    if effort := info.effort(cfg.effort):
        body["reasoning"] = {"effort": effort}
    return body


class LLMGateway:
    def __init__(
        self,
        run_id: str,
        repo_id: int,
        client: OpenRouter | None = None,
        run_budget_usd: float | None = None,
    ) -> None:
        """`run_budget_usd` caps this run's spend; it defaults to the per-report cap."""
        self.run_id = run_id
        self.repo_id = repo_id
        self.settings = get_settings()
        self.client = client or make_client()
        self.run_budget_usd = run_budget_usd if run_budget_usd is not None else self.settings.per_report_budget_usd
        self._last_cost = 0.0
        self.last_model = ""  # the model that gave the most recent answer

    @property
    def last_cost(self) -> float:
        """Cost of the most recent call; 0 when it was answered from the cache."""
        return self._last_cost

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
        if run + pending > self.run_budget_usd:
            raise BudgetExceeded(f"Run budget reached (${run:.2f} spent of ${self.run_budget_usd:.2f}).")

    async def _cached(self, agent: str, key: str, schema: type[T]) -> T | None:
        async with session_scope() as s:
            row = (
                await s.execute(select(AgentVerdict).where(AgentVerdict.agent == agent, AgentVerdict.input_hash == key))
            ).scalar_one_or_none()
        return schema.model_validate(row.output) if row else None

    async def _send(self, agent: str, info: ModelInfo, body: dict[str, Any], estimate: float) -> dict[str, Any]:
        """One request, checked against the controls first and recorded in the ledger after."""
        global _in_flight_usd
        await self._check_controls(estimate)
        ledger = LLMCall(run_id=self.run_id, agent=agent, model=info.id, layers=_layers.get())
        _in_flight_usd += estimate
        started = time.monotonic()
        try:
            data = await self.client.complete(body)
        except ModelAPIError as e:
            ledger.error = str(e)[:2000]
            async with session_scope() as s:
                s.add(ledger)
            raise
        finally:
            _in_flight_usd -= estimate
        choice = (data.get("choices") or [{}])[0]
        await _record(ledger, data, info, choice.get("finish_reason"), started)
        if (spent := _spent.get()) is not None:
            spent.append(ledger.cost_usd)
        return data

    async def parse(
        self,
        *,
        agent: str,
        cfg: AgentModel,
        system: str,
        user: str | Prompt,
        schema: type[T],
        cache_key_parts: tuple[Any, ...],
        label: str = "",
        route: RouteHint | None = None,
    ) -> T:
        """One structured answer validated against `schema`, from the first model on the route that
        gives one. `label` says what the call is about (e.g. "PR #12") for the Jobs page. A `Prompt` user
        message is sent in its layers, the stable ones marked for the provider's cache."""
        self._last_cost = 0.0
        content = user if isinstance(user, str) else user.blocks()
        key = input_hash(agent, cfg.model, system, content, schema.model_json_schema(), *cache_key_parts)
        if (hit := await self._cached(agent, key, schema)) is not None:
            log.debug("verdict_cache_hit", agent=agent)
            await events.record(self.run_id, "llm_end", f"{agent}: {label} (cached)", agent=agent, cached=True)
            return hit

        candidates = await routes(cfg, route, structured=True, agent=agent)
        spent: list[float] = []
        token = _spent.set(spent)
        layers = _layers.set(None if isinstance(user, str) else user.report())
        try:
            result, used = await self._first_answer(agent, label, cfg, candidates, system, content, schema)
        finally:
            _spent.reset(token)
            _layers.reset(layers)
            cost = self._last_cost = round(sum(spent), 5)
        self.last_model = used.id
        await events.record(self.run_id, "llm_end", f"{agent}: {label}", agent=agent, model=used.id, cost_usd=cost)
        async with session_scope() as s:
            s.add(
                AgentVerdict(
                    repo_id=self.repo_id, agent=agent, input_hash=key, output=result.model_dump(mode="json"),
                    model=used.id,
                )
            )  # fmt: skip
        log.info("llm_call", agent=agent, model=used.id, cost_usd=cost)
        return result

    async def _first_answer(
        self,
        agent: str,
        label: str,
        cfg: AgentModel,
        candidates: list[Route],
        system: str,
        user: str | list[dict[str, Any]],
        schema: type[T],
    ) -> tuple[T, ModelInfo]:
        """The answer of the first model on the route that gives a usable one."""
        for n, r in enumerate(candidates):
            await self._start(agent, label, r)
            try:
                return await self._structured(agent, cfg, r.info, system, user, schema), r.info
            except (ModelAPIError, ModelRefused, AgentOutputError) as e:
                await self._failed(agent, label, r, e, candidates[n + 1] if n + 1 < len(candidates) else None)
        raise AssertionError("unreachable: the last route either answers or raises")

    async def _start(self, agent: str, label: str, r: Route) -> None:
        await events.record(
            self.run_id, "llm_start", f"{agent}: {label} on {r.info.id} ({r.why})", agent=agent, model=r.info.id
        )

    async def _failed(self, agent: str, label: str, r: Route, e: Exception, after: Route | None) -> None:
        """Move on to the next model on the route, or re-raise when there is none or it would not help."""
        account = isinstance(e, ModelAPIError) and e.status in (401, 402, 403)  # the key or credits, not the model
        if not account:  # API errors are in the ledger too; the "api:" prefix keeps them from counting twice
            kind = "api" if isinstance(e, ModelAPIError) else type(e).__name__
            await outcomes.record(agent, r.info.id, ok=False, kind="call", run_id=self.run_id, detail=f"{kind}: {e}")
        if after is None or account:
            await events.record(self.run_id, "warning", f"{agent}: {label} failed on {r.info.id}: {e}", agent=agent)
            raise e
        await events.record(
            self.run_id, events.CAUGHT, f"{agent}: {r.info.id} failed ({e}); trying {after.info.id}", agent=agent
        )
        log.warning("model_fallback", agent=agent, failed=r.info.id, next=after.info.id, error=str(e)[:300])

    async def _structured(
        self,
        agent: str,
        cfg: AgentModel,
        info: ModelInfo,
        system: str,
        user: str | list[dict[str, Any]],
        schema: type[T],
    ) -> T:
        """One model's structured answer. Output that does not validate gets one repair turn."""
        # The system prompt repeats across calls, and so do a layered prompt's repo and item layers (marked by
        # prompt.Prompt); caching a plain, unique user message would only add write cost.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": user},
        ]
        shape = strict_schema(schema.model_json_schema())
        fmt = {"type": "json_schema", "json_schema": {"name": schema.__name__, "strict": True, "schema": shape}}
        chars = len(user) if isinstance(user, str) else sum(len(b["text"]) for b in user)
        estimate = estimate_cost(info, cfg, len(system) + chars)
        for attempt in (1, 2):
            body = {**_request(info, cfg, messages), "response_format": fmt}
            data = await self._send(agent, info, body, estimate)
            choice = data["choices"][0]
            turn = to_turn(choice["message"], choice.get("finish_reason"), data.get("usage") or {}, info.id)
            if turn.stop_reason == "refusal":
                raise ModelRefused(f"{agent} request was declined by {info.id}.")
            if turn.stop_reason == "max_tokens":
                raise AgentOutputError(f"{agent} output was cut off at max_tokens ({cfg.max_tokens}).")
            text = "".join(b.text or "" for b in turn.content if b.type == "text")
            try:
                return schema.model_validate_json(_strip_fence(text))
            except ValidationError as e:
                if attempt == 2:
                    raise AgentOutputError(f"{agent} returned output that does not match its schema: {e}") from e
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That did not match the schema:\n{e}\nAnswer again, only JSON."},
                ]
        raise AssertionError("unreachable")

    async def create(
        self,
        *,
        agent: str,
        cfg: AgentModel,
        system: list[TextBlockParam],
        messages: list[MessageParam],
        tools: list[ToolParam],
        label: str = "",
        route: RouteHint | None = None,
    ) -> Turn:
        """One turn of a tool-using agent (not cached: each turn is unique), from the first model on the
        route that answers. A refusal moves on to the next model too; the last one's refusal is returned."""
        self._last_cost = 0.0
        candidates = await routes(cfg, route, tools=True, agent=agent)
        spent: list[float] = []
        token = _spent.set(spent)
        try:
            turn = await self._first_turn(agent, label, cfg, candidates, system, messages, tools)
        finally:
            _spent.reset(token)
            cost = self._last_cost = round(sum(spent), 5)
        self.last_model = turn.model
        await events.record(self.run_id, "llm_end", f"{agent}: {label}", agent=agent, model=turn.model, cost_usd=cost)
        return turn

    async def _first_turn(
        self,
        agent: str,
        label: str,
        cfg: AgentModel,
        candidates: list[Route],
        system: list[TextBlockParam],
        messages: list[MessageParam],
        tools: list[ToolParam],
    ) -> Turn:
        for n, r in enumerate(candidates):
            after = candidates[n + 1] if n + 1 < len(candidates) else None
            body = {**_request(r.info, cfg, to_wire(system, messages, r.info.id)), "tools": to_wire_tools(tools)}
            estimate = estimate_cost(r.info, cfg, len(json.dumps(body["messages"], default=str)))
            await self._start(agent, label, r)
            try:
                data = await self._send(agent, r.info, body, estimate)
            except ModelAPIError as e:
                await self._failed(agent, label, r, e, after)
                continue
            choice = data["choices"][0]
            turn = to_turn(choice["message"], choice.get("finish_reason"), data.get("usage") or {}, r.info.id)
            turn.provider = data.get("provider")
            if turn.stop_reason == "refusal" and after:
                await self._failed(agent, label, r, ModelRefused(f"declined by {r.info.id}"), after)
                continue
            return turn
        raise AssertionError("unreachable: the last route either returns or raises")


def _strip_fence(text: str) -> str:
    """Some models wrap JSON in a Markdown code fence even in structured-output mode."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


async def _record(
    ledger: LLMCall, data: dict[str, Any], info: ModelInfo, finish_reason: str | None, started: float
) -> None:
    """Fill the ledger row from a response's usage and save it."""
    usage = data.get("usage") or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    ledger.request_id = (data.get("id") or "")[:80] or None
    ledger.provider = (data.get("provider") or "")[:60] or None
    ledger.stop_reason = STOP_REASONS.get(finish_reason or "", "end_turn")
    ledger.input_tokens = usage.get("prompt_tokens") or 0
    ledger.output_tokens = usage.get("completion_tokens") or 0
    ledger.cache_read_tokens = cached
    ledger.cache_write_tokens = (usage.get("prompt_tokens_details") or {}).get("cache_write_tokens") or 0
    # What OpenRouter billed; the catalog price is only a fallback when it is missing.
    billed = usage.get("cost")
    estimated = info.cost(ledger.input_tokens, ledger.output_tokens, cached)
    ledger.cost_usd = float(billed) if billed is not None else estimated
    ledger.latency_ms = int((time.monotonic() - started) * 1000)
    async with session_scope() as s:
        s.add(ledger)


class ChatStream:
    """One streamed chat call. Iterate for text deltas; `message` holds the final Turn afterwards."""

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
        self.message: Turn | None = None
        self.cost = 0.0

    async def __aiter__(self) -> AsyncIterator[str]:
        global _in_flight_usd
        gw, cfg = self.gateway, self.cfg
        info = (await routes(cfg, tools=True))[0].info  # no fallback once text has streamed to the reader
        body = {**_request(info, cfg, to_wire(self.system, self.messages, info.id)), "tools": to_wire_tools(self.tools)}
        estimate = estimate_cost(info, cfg, len(json.dumps(body["messages"], default=str)))
        await gw._check_controls(estimate)
        ledger = LLMCall(run_id=gw.run_id, agent=self.agent, model=info.id)
        text, calls, reasoning = "", {}, []  # type: ignore[var-annotated]
        finish, usage, meta = None, {}, {}  # type: ignore[var-annotated]
        _in_flight_usd += estimate
        started = time.monotonic()
        try:
            async for chunk in gw.client.stream(body):
                meta = {"id": chunk.get("id", meta.get("id")), "provider": chunk.get("provider", meta.get("provider"))}
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    finish = choice.get("finish_reason") or finish
                    if delta.get("content"):
                        text += delta["content"]
                        yield delta["content"]
                    reasoning += delta.get("reasoning_details") or []
                    for tc in delta.get("tool_calls") or []:
                        call = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        call["id"] = tc.get("id") or call["id"]
                        call["name"] = fn.get("name") or call["name"]
                        call["arguments"] += fn.get("arguments") or ""
        except ModelAPIError as e:
            ledger.error = str(e)[:2000]
            async with session_scope() as s:
                s.add(ledger)
            raise
        finally:
            _in_flight_usd -= estimate
        message = {
            "content": text,
            "tool_calls": [
                {"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"]}}
                for _, c in sorted(calls.items())
            ],
            "reasoning_details": _merge_reasoning(reasoning),
        }
        self.message = to_turn(message, finish, usage, info.id)
        self.message.provider = meta.get("provider")
        await _record(ledger, {**meta, "usage": usage}, info, finish, started)
        self.cost = ledger.cost_usd


def _merge_reasoning(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Streamed reasoning arrives in pieces, each with the index of the item it belongs to."""
    merged: dict[int, dict[str, Any]] = {}
    for p in parts:
        item = merged.setdefault(p.get("index", 0), {})
        for k, v in p.items():
            if k in ("text", "summary", "data") and isinstance(v, str):
                item[k] = item.get(k, "") + v
            elif v is not None:
                item[k] = v  # type, format, signature: whichever piece carries them
    return [merged[i] for i in sorted(merged)]


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
