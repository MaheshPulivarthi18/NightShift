"""Runtime configuration, loaded from environment variables and `.env`."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


def model_id(name: str) -> str:
    """An OpenRouter model id. Old Claude names are accepted: claude-opus-5-5 -> anthropic/claude-opus-5.5"""
    if "/" in name:
        return name
    return "anthropic/" + re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", name)


class AgentModel(BaseModel):
    """Which models an agent uses (any OpenRouter model ids), and how hard it thinks.

    `model` is the first choice. `ladder` lists the models the router moves to, in order: when the
    first choice fails or refuses, when a task is large, or when an earlier attempt did not work."""

    model: str
    ladder: list[str] = []
    effort: Effort = "medium"
    max_tokens: int = 16000

    @field_validator("model")
    @classmethod
    def _openrouter_id(cls, v: str) -> str:
        return model_id(v)

    @field_validator("ladder")
    @classmethod
    def _openrouter_ids(cls, v: list[str]) -> list[str]:
        return [model_id(m) for m in v]

    @property
    def models(self) -> list[str]:
        return list(dict.fromkeys([self.model, *self.ladder]))


# Default models (OpenRouter ids).
OPUS = "anthropic/claude-opus-5.5"
SONNET = "anthropic/claude-sonnet-5.5"
SOL = "openai/gpt-5.6-sol"
TERRA = "openai/gpt-5.6-terra"
LUNA = "openai/gpt-5.6-luna"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NIGHTSHIFT_", env_file=".env", env_nested_delimiter="__", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://nightshift:nightshift@localhost:5432/nightshift"

    # Every model call goes through OpenRouter, whatever the model's vendor.
    openrouter_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Read-only token for public data. Never a token that can write.
    github_token: SecretStr = Field(default=SecretStr(""), validation_alias="GITHUB_TOKEN")
    github_api_url: str = "https://api.github.com/graphql"
    # The operator's GitHub login: their own assignments and PRs do not count as someone else's claim.
    github_login: str = ""

    # Per-agent models: any OpenRouter model ids. The first is tried first; the router moves along the
    # ladder on failure, for large tasks and on escalation (see router.py). Override with e.g.
    # NIGHTSHIFT_CODER__MODEL=openai/gpt-5.6-sol or NIGHTSHIFT_CODER__LADDER='["anthropic/claude-opus-5.5"]'.
    # Light, high-volume jobs start on a cheap model; judgement-heavy ones on Sonnet with Opus above it.
    relevance: AgentModel = AgentModel(model=LUNA, ladder=[SONNET], effort="low")
    pr_assessor: AgentModel = AgentModel(model=SONNET, ladder=[OPUS], effort="medium")
    verifier: AgentModel = AgentModel(model=SONNET, ladder=[OPUS], effort="medium")
    scout: AgentModel = AgentModel(model=LUNA, ladder=[SONNET], effort="low")
    briefer: AgentModel = AgentModel(model=LUNA, ladder=[SONNET], effort="low")
    chat: AgentModel = AgentModel(model=SONNET, ladder=[SOL], effort="medium", max_tokens=8000)
    cartographer: AgentModel = AgentModel(model=SONNET, ladder=[TERRA], effort="medium", max_tokens=8000)
    # Contribution Worker. The Reviewer is from another vendor than the Coder when the ladder allows:
    # a second family does not share the first one's blind spots.
    setup: AgentModel = AgentModel(model=LUNA, ladder=[SONNET], effort="medium", max_tokens=8000)
    planner: AgentModel = AgentModel(model=SONNET, ladder=[OPUS], effort="medium", max_tokens=12000)
    coder: AgentModel = AgentModel(model=SONNET, ladder=[OPUS], effort="medium", max_tokens=16000)
    reviewer: AgentModel = AgentModel(model=SOL, ladder=[SONNET, OPUS], effort="high", max_tokens=20000)
    pr_writer: AgentModel = AgentModel(model=SONNET, ladder=[TERRA], effort="low", max_tokens=4000)
    # PR checker (maintainer side): proposes how to run a PR's tests and the linked issue's reproduction.
    pr_checker: AgentModel = AgentModel(model=SONNET, ladder=[OPUS], effort="medium", max_tokens=8000)
    # Plan check before Gate 1 (worker/plancheck.py): reads the plan against the code. Another family than
    # the Planner's by default, like the Reviewer, so it does not share the Planner's blind spots. Luna found
    # the same gaps as Sol in the two plans it was tried on (multilspy #101, laya #6) at about 1/22 the cost.
    plan_checker: AgentModel = AgentModel(model=LUNA, ladder=[SOL], effort="medium", max_tokens=8000)
    # Writes the understanding check's questions about a change, and grades the contributor's answers.
    tutor: AgentModel = AgentModel(model=LUNA, ladder=[SOL], effort="medium", max_tokens=6000)
    # Last line of every commit the worker publishes. Empty to leave it out.
    commit_attribution: str = "Generated by NightShift"
    branch_prefix: str = "nightshift"  # branches pushed to the operator's fork: <prefix>/<issue>-<slug>

    # Per-task limits; a task that hits one stops with its work saved.
    task_budget_usd: float = 5.0
    task_minutes: int = 30
    coder_max_steps: int = 60
    review_rounds: int = 2
    # The router reorders each agent's ladder from past outcomes (see outcomes.py). False: config order only.
    router_learning: bool = True
    # Setup agent: tries an environment for a repo this many times, within this budget, once per repo.
    setup_attempts: int = 5
    setup_budget_usd: float = 1.0
    pr_check_budget_usd: float = 1.5  # per checked PR, the Setup agent included
    setup_test_seconds: int = 300  # per test command while finding an environment: big suites must be scoped
    # An environment image build past this is stopped: one ran 20 silent minutes (multilspy) before this limit
    image_build_seconds: int = 600
    # One model request may take this long before it is given up and the next model on the route is tried.
    # OpenRouter keeps a slow request alive with blank lines, so the read timeout alone never fires: a review
    # call once hung over 15 minutes. Reviews usually take under a minute.
    model_call_seconds: int = 300
    # Changed lines (tests and generated files left out) past which a change is out of scope for the plan's size.
    # Outside contributors' PRs maintainers merged (pydantic, opentelemetry-python, 20): median 8, 90% under 190,
    # largest 376, so these only catch a change that grew far past its plan.
    scope_limits: dict[str, int] = {"small": 250, "medium": 600, "large": 2500}

    # Task sandboxes (Docker). Build the base image with `nightshift sandbox build`.
    sandbox_image: str = "nightshift-sandbox:3"
    # Background sync of your published PRs' feedback (worker/sync.py): minutes between runs; 0 = only Sync now.
    sync_minutes: int = 60
    watchlist_max: int = 30  # repos on the watchlist; adding one past it is refused (CLI and web)
    sandbox_cpus: float = 2.0
    sandbox_memory: str = "4g"
    sandbox_pids: int = 1024
    sandbox_command_timeout: int = 900  # seconds per command

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
