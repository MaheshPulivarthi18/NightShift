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
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "maintainer": backlog triage report. "contributor": where an outsider can usefully help.
    role: Mapped[str] = mapped_column(String(20), default="contributor", server_default="contributor")
    # Bumped when the fetched fields change, so old repos get one full refetch.
    fetch_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list[Item]] = relationship(back_populates="repo")


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
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


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
