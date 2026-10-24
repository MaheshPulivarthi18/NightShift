"""OpenRouter's model catalog: what each model costs, how much it reads, what it can do, how it scores.

Fetched from OpenRouter's /models list at most once a day and kept in the database, so a run still
knows its models when OpenRouter's list cannot be reached. The gateway uses it to check that a model
can do an agent's job (tool calls for the Coder, structured answers for the Planner) and to estimate a
call's cost before the budget check; the router will use the benchmark scores.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from pydantic import BaseModel
from sqlalchemy import delete, select

from nightshift.config import get_settings
from nightshift.db import ModelCatalog, session_scope

log = structlog.get_logger()

MAX_AGE = timedelta(hours=24)
EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class ModelConfigError(RuntimeError):
    """The configured model is not on OpenRouter, or cannot do what the agent needs."""


class ModelInfo(BaseModel):
    id: str
    name: str = ""
    context_length: int = 0
    max_output: int | None = None
    # USD per token.
    prompt: float = 0.0
    completion: float = 0.0
    cache_read: float = 0.0
    supported: list[str] = []
    efforts: list[str] = []  # reasoning efforts the model accepts; empty if it has no reasoning setting
    # Artificial Analysis indexes, when OpenRouter has them.
    intelligence: float | None = None
    coding: float | None = None
    agentic: float | None = None

    @classmethod
    def from_openrouter(cls, m: dict[str, Any]) -> ModelInfo:
        price = m.get("pricing") or {}
        top = m.get("top_provider") or {}
        scores = ((m.get("benchmarks") or {}).get("artificial_analysis")) or {}
        return cls(
            id=m["id"],
            name=m.get("name", ""),
            context_length=m.get("context_length") or 0,
            max_output=top.get("max_completion_tokens"),
            prompt=float(price.get("prompt") or 0),
            completion=float(price.get("completion") or 0),
            cache_read=float(price.get("input_cache_read") or 0),
            supported=m.get("supported_parameters") or [],
            efforts=(m.get("reasoning") or {}).get("supported_efforts") or [],
            intelligence=scores.get("intelligence_index"),
            coding=scores.get("coding_index"),
            agentic=scores.get("agentic_index"),
        )

    @property
    def tools(self) -> bool:
        return "tools" in self.supported

    @property
    def structured(self) -> bool:
        return "structured_outputs" in self.supported

    def effort(self, wanted: str) -> str | None:
        """The reasoning effort to send: the wanted one, else the nearest the model accepts, else none."""
        if "reasoning" not in self.supported:
            return None
        if not self.efforts or wanted in self.efforts:
            return wanted
        rank = EFFORTS.index(wanted) if wanted in EFFORTS else EFFORTS.index("medium")
        known = [e for e in self.efforts if e in EFFORTS]
        return min(known, key=lambda e: abs(EFFORTS.index(e) - rank)) if known else None

    def cost(self, input_tokens: float, output_tokens: float, cached_tokens: float = 0) -> float:
        fresh = max(0.0, input_tokens - cached_tokens)
        return fresh * self.prompt + cached_tokens * self.cache_read + output_tokens * self.completion


_models: dict[str, ModelInfo] = {}
_loaded_at: datetime | None = None


async def fetch(http: httpx.AsyncClient | None = None) -> list[ModelInfo]:
    """OpenRouter's current model list (public; no key needed)."""
    url = get_settings().openrouter_base_url.rstrip("/") + "/models"
    if http is None:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(url)
    else:
        res = await http.get(url)
    res.raise_for_status()
    return [ModelInfo.from_openrouter(m) for m in res.json()["data"]]


async def refresh(http: httpx.AsyncClient | None = None) -> int:
    """Fetch the catalog and replace the stored copy. Returns the number of models."""
    global _models, _loaded_at
    models = await fetch(http)
    now = datetime.now(UTC)
    async with session_scope() as s:
        await s.execute(delete(ModelCatalog))
        s.add_all(ModelCatalog(id=m.id, data=m.model_dump(mode="json"), fetched_at=now) for m in models)
    _models, _loaded_at = {m.id: m for m in models}, now
    log.info("model_catalog_refreshed", models=len(models))
    return len(models)


async def _load() -> None:
    """Fill the in-process copy from the database, refreshing from OpenRouter when it is a day old."""
    global _models, _loaded_at
    if _loaded_at and datetime.now(UTC) - _loaded_at < MAX_AGE:
        return
    async with session_scope() as s:
        rows = list((await s.execute(select(ModelCatalog))).scalars())
    if rows:
        _models = {r.id: ModelInfo.model_validate(r.data) for r in rows}
        _loaded_at = min(r.fetched_at for r in rows)
    if not rows or datetime.now(UTC) - (_loaded_at or datetime.min.replace(tzinfo=UTC)) >= MAX_AGE:
        try:
            await refresh()
        except (httpx.HTTPError, KeyError, ValueError) as e:
            if not rows:
                raise ModelConfigError(f"Cannot read OpenRouter's model list: {e}") from e
            log.warning("model_catalog_stale", error=str(e))  # keep using yesterday's copy
            _loaded_at = datetime.now(UTC)


async def model_info(model: str) -> ModelInfo | None:
    await _load()
    return _models.get(model)


async def all_models() -> list[ModelInfo]:
    await _load()
    return list(_models.values())


async def require(model: str, *, tools: bool = False, structured: bool = False) -> ModelInfo:
    """The model's catalog entry, or a ModelConfigError saying why it cannot do this agent's job."""
    info = await model_info(model)
    if info is None:
        raise ModelConfigError(f"{model} is not on OpenRouter. Check the model id in your settings.")
    if tools and not info.tools:
        raise ModelConfigError(f"{model} cannot call tools, which this agent needs.")
    if structured and not info.structured:
        raise ModelConfigError(f"{model} cannot return structured output, which this agent needs.")
    return info
