"""Runtime configuration, loaded from environment variables and `.env`."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class AgentModel(BaseModel):
    """Which Claude model an agent uses, and how hard it thinks."""

    model: str
    effort: Effort = "medium"
    max_tokens: int = 16000


class ModelPrice(BaseModel):
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


# First-party API rates, used for the spend ledger and budget checks.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
    "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0, cache_read=0.20, cache_write=2.5),
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
}

# Models that accept the server-side `fallbacks: "default"` refusal fallback.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


def openrouter_model(model: str) -> str:
    """claude-opus-5-5 -> anthropic/claude-opus-5.5"""
    return "anthropic/" + re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NIGHTSHIFT_", env_file=".env", env_nested_delimiter="__", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://nightshift:nightshift@localhost:5432/nightshift"

    # Where Claude calls go. "openrouter" uses OpenRouter's Anthropic-compatible endpoint.
    provider: Literal["anthropic", "openrouter"] = "anthropic"
    openrouter_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = "https://openrouter.ai/api"

    # Read-only token for public data. Never a token that can write.
    github_token: SecretStr = Field(default=SecretStr(""), validation_alias="GITHUB_TOKEN")
    github_api_url: str = "https://api.github.com/graphql"

    # Per-agent model choice. Override with e.g. NIGHTSHIFT_RELEVANCE__MODEL=claude-haiku-5-5
    relevance: AgentModel = AgentModel(model="claude-opus-5-5", effort="low")
    pr_assessor: AgentModel = AgentModel(model="claude-opus-5-5", effort="medium")
    verifier: AgentModel = AgentModel(model="claude-opus-5-5", effort="medium")
    scout: AgentModel = AgentModel(model="claude-opus-5-5", effort="low")

    # Hard spend caps, checked before every model call.
    daily_budget_usd: float = 1.25
    monthly_budget_usd: float = 40.0
    per_report_budget_usd: float = 5.0

    # Report shaping.
    max_items_per_run: int = 1000
    closed_lookback_days: int = 90
    abandoned_after_days: int = 60
    max_group_size: int = 8
    similarity_threshold: float = 0.82
    llm_concurrency: int = 4

    # Contributor report.
    pulse_window_days: int = 90
    max_opportunities: int = 30

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    reports_dir: str = "reports"


@lru_cache
def get_settings() -> Settings:
    return Settings()
