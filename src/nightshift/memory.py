"""Long-term repo memory: items, embeddings, feedback.

Embeddings come from a small local model (fastembed, ONNX), so similarity search costs
nothing per call. Vectors live in Postgres (pgvector) next to the items they describe.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nightshift.config import get_settings
from nightshift.db import Feedback, Item, Repo

log = structlog.get_logger()


def content_hash(item: dict[str, Any]) -> str:
    keep = {k: item[k] for k in ("title", "body", "state", "state_reason", "labels", "data")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def embed_text(item: Item) -> str:
    return f"{item.title}\n\n{item.body[:1500]}"


@lru_cache
def _embedder() -> Any:
    from fastembed import TextEmbedding  # heavy import; load once, on first use

    return TextEmbedding(model_name=get_settings().embedding_model)


def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return [vec.tolist() for vec in _embedder().embed(texts, batch_size=32)]


async def get_or_create_repo(
    s: AsyncSession, full_name: str, connected: bool = False, role: str = "contributor"
) -> Repo:
    repo = (await s.execute(select(Repo).where(Repo.full_name == full_name))).scalar_one_or_none()
    if repo is None:
        repo = Repo(full_name=full_name, connected=connected, role=role)
        s.add(repo)
        await s.flush()
    return repo


async def upsert_items(s: AsyncSession, repo: Repo, fetched: list[dict[str, Any]]) -> int:
    """Store fetched items; return how many were new or changed."""
    existing = {it.number: it for it in (await s.execute(select(Item).where(Item.repo_id == repo.id))).scalars()}
    changed = 0
    for raw in fetched:
        h = content_hash(raw)
        item = existing.get(raw["number"])
        if item is not None and item.content_hash == h:
            continue
        changed += 1
        if item is None:
            item = Item(repo_id=repo.id, number=raw["number"])
            s.add(item)
            existing[raw["number"]] = item
        for field in (
            "kind",
            "title",
            "body",
            "author",
            "state",
            "state_reason",
            "url",
            "labels",
            "created_at",
            "updated_at",
            "closed_at",
            "data",
        ):
            setattr(item, field, raw[field])
        item.content_hash = h
    repo.last_fetched_at = datetime.now(UTC)
    await s.flush()
    return changed


async def refresh_embeddings(s: AsyncSession, repo_id: int) -> int:
    """Embed items whose text changed since they were last embedded."""
    items = (await s.execute(select(Item).where(Item.repo_id == repo_id))).scalars().all()
    stale = [it for it in items if it.embedded_hash != it.content_hash]
    vectors = await asyncio.to_thread(embed, [embed_text(it) for it in stale])
    for it, vec in zip(stale, vectors, strict=True):
        it.embedding = vec
        it.embedded_hash = it.content_hash
    await s.flush()
    return len(stale)


async def nearest(s: AsyncSession, item: Item, k: int) -> list[tuple[Item, float]]:
    """k most similar items in the same repo, with cosine similarity."""
    if item.embedding is None:
        return []
    distance = Item.embedding.cosine_distance(item.embedding)
    rows = await s.execute(
        select(Item, distance.label("d"))
        .where(Item.repo_id == item.repo_id, Item.id != item.id, Item.embedding.is_not(None))
        .order_by(distance)
        .limit(k)
    )
    return [(other, 1.0 - float(d)) for other, d in rows.all()]


async def feedback_examples(s: AsyncSession, repo_id: int, limit: int = 6) -> list[dict[str, Any]]:
    """Recent operator judgements on this repo, newest first, for few-shot grounding."""
    rows = (
        await s.execute(
            select(Feedback).where(Feedback.repo_id == repo_id).order_by(Feedback.created_at.desc()).limit(limit)
        )
    ).scalars()
    return [{"finding": f.finding, "operator_says_correct": f.correct, "note": f.note} for f in rows]
