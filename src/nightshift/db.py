"""Database models and session handling (Postgres + pgvector)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from nightshift.config import get_settings

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Repo(Base):
    """A repo NightShift knows about. `connected` repos have the App installed; others are watched."""

    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), unique=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    # False: removed from the watchlist in the UI. Its tasks and history stay; adding it again restores it.
    watched: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # How Gate 2 publishes here, remembered from it: {"attribution": str, "branch_prefix": str}. A missing key
    # means the global setting (commit_attribution, branch_prefix).
    publish: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, server_default=text("'{}'"))
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "maintainer": backlog triage report. "contributor": where an outsider can usefully help.
    role: Mapped[str] = mapped_column(String(20), default="contributor", server_default="contributor")
    # Check others' open PRs here on every background sync (worker/autocheck.py); a maintainer's opt-in.
    auto_check: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Bumped when the fetched fields change, so old repos get one full refetch.
    fetch_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Deleting a repo leaves its rows to the foreign keys' ON DELETE CASCADE, rather than nulling repo_id.
    items: Mapped[list[Item]] = relationship(back_populates="repo", passive_deletes=True)


class Item(Base):
    """An issue or pull request, as last fetched. Long-term repo memory."""

    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("repo_id", "number"), Index("ix_items_repo_state", "repo_id", "state"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))  # "issue" | "pr"
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20))  # OPEN | CLOSED | MERGED
    state_reason: Mapped[str | None] = mapped_column(String(30))
    url: Mapped[str] = mapped_column(Text)
    labels: Mapped[list[str]] = mapped_column(JsonType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Everything else the analysis needs: comments, PR signals, links. Shape in github/fetch.py.
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(get_settings().embedding_dim))
    embedded_hash: Mapped[str | None] = mapped_column(String(64))

    repo: Mapped[Repo] = relationship(back_populates="items")

    @property
    def ref(self) -> str:
        return f"#{self.number}"


class AgentVerdict(Base):
    """Cache of agent outputs keyed by a hash of their exact inputs."""

    __tablename__ = "agent_verdicts"
    __table_args__ = (UniqueConstraint("agent", "input_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    agent: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    output: Mapped[dict[str, Any]] = mapped_column(JsonType)
    model: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LLMCall(Base):
    """Spend ledger: one row per model call."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(60))
    request_id: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    stop_reason: Mapped[str | None] = mapped_column(String(30))
    provider: Mapped[str | None] = mapped_column(String(60))  # who OpenRouter sent the call to
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    # A layered prompt's size per layer and what was cut (prompt.Prompt.report); None for a plain one.
    layers: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PRCheck(Base):
    """A pull request checked by the PR checker (worker/prcheck.py): its verdict and the full report."""

    __tablename__ = "pr_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(100), default="")
    head_sha: Mapped[str] = mapped_column(String(40), default="")
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running, done, failed
    verdict: Mapped[str | None] = mapped_column(String(60))
    must: Mapped[int] = mapped_column(Integer, default=0)
    should: Mapped[int] = mapped_column(Integer, default=0)
    markdown: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    review_url: Mapped[str | None] = mapped_column(String(300))  # the review the operator posted from it
    auto: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # started by the sync
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # not to be posted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ModelOutcome(Base):
    """How a model did at an agent's job: a task outcome (the Coder's fix verified, the plan approved,
    the environment ready) or a failed call (an API error, a refusal, unusable output). The router
    learns its ordering from these."""

    __tablename__ = "model_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(120))
    run_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))  # "task" or "call"
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ModelCatalog(Base):
    """OpenRouter's model list: price, context, capabilities and benchmark scores, refreshed daily."""

    __tablename__ = "model_catalog"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JsonType)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Report(Base):
    """A health report snapshot. `findings` holds the verified, structured result."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True)
    findings: Mapped[dict[str, Any]] = mapped_column(JsonType)
    markdown: Mapped[str] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Feedback(Base):
    """The operator's right/wrong mark on a finding. Fed back to agents as examples."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    finding_key: Mapped[str] = mapped_column(String(120))
    correct: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text)
    finding: Mapped[dict[str, Any]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Control(Base):
    """Fleet-wide switches. A single row with id=1."""

    __tablename__ = "control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    killed: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Run(Base):
    """One report run, for the Jobs page. Written by whichever process runs it (CLI or web)."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | done | stopped | failed
    current_node: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    report_path: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Written every events.BEAT by the process running it: when it stops, that process is gone
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    """A step in a run: a node starting or finishing, a model call starting or finishing, a warning."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    # node_start | node_end | llm_start | llm_end | caught (a check found a problem; NightShift is handling it)
    # | fixed (it was handled) | warning (a problem nothing handles) | error
    kind: Mapped[str] = mapped_column(String(20))
    node: Mapped[str | None] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chat(Base):
    """A conversation about one issue or PR."""

    __tablename__ = "chats"
    __table_args__ = (UniqueConstraint("repo_id", "number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatMessage(Base):
    """One turn of a chat. `content` is the Messages API content (text, tool_use and tool_result blocks)."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepoContext(Base):
    """Context computed for a repo at one commit, e.g. the L1 architecture map. Reused by every task there."""

    __tablename__ = "repo_context"
    __table_args__ = (UniqueConstraint("repo_id", "kind", "commit"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # architecture_map
    commit: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JsonType)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Base):
    """One contribution: an issue taken from plan, through the operator's approval, to a reviewed fix.

    status: planned (waiting at Gate 1) | approved | rejected | working | ready (fix verified and
    reviewed, PR drafted: waiting at Gate 2) | published | needs_operator (stopped with work saved) | failed
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    issue_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="planned")
    base_commit: Mapped[str] = mapped_column(String(40))
    issue: Mapped[dict[str, Any]] = mapped_column(JsonType)  # the issue as fetched when planning
    plan: Mapped[dict[str, Any]] = mapped_column(JsonType)
    result: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)  # baseline, checks, reviews
    diff: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)  # the operator's note at the gate
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # off the board, kept
    # Written every half minute while planning or running; a task whose run died stops getting it.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def run_id(self) -> str:
        return f"task-{self.id}"


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session that commits on success and rolls back on error."""
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
