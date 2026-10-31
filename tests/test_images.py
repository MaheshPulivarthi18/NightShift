"""Sandbox images: each repo keeps its newest few; a clean-up removes what nothing needs, never what a sandbox
uses. Docker is faked; the repo list comes from the test database."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete

from nightshift.db import Repo, session_scope
from nightshift.sandbox import images
from nightshift.sandbox.images import _stale, size_bytes


def test_sizes_and_what_is_stale() -> None:
    assert size_bytes("3.03GB") == 3_030_000_000 and size_bytes("512MB") == 512_000_000 and size_bytes("") == 0
    tags = ["r:new", "r:mid", "r:old", "r:older"]  # newest first, as Docker lists them
    assert _stale(tags, 2, busy=set()) == ["r:old", "r:older"]
    assert _stale(tags, 2, busy={"r:old"}) == ["r:older"]  # a sandbox still runs from it


@pytest.fixture
async def repo_name() -> AsyncIterator[str]:
    name = f"test-images/{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            s.add(Repo(full_name=name))
    except OSError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    yield name
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_a_clean_up_keeps_what_work_needs(repo_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = "nightshift-repo/" + repo_name.replace("/", "__")
    listed = [
        *({"tag": f"{mine}:{k}", "id": k, "size": 3_000_000_000, "created": ""} for k in ("a", "b", "c", "d")),
        {"tag": "nightshift-repo/gone__repo:x", "id": "x", "size": 1, "created": ""},  # a repo no longer stored
        {"tag": "nightshift-env:used", "id": "e1", "size": 1, "created": ""},
        {"tag": "nightshift-env:unused", "id": "e2", "size": 1, "created": ""},
        {"tag": "nightshift-partial/gone__repo:k", "id": "p", "size": 1, "created": ""},
    ]
    removed: list[str] = []

    async def fake_images() -> list[dict[str, object]]:
        return listed

    async def busy() -> set[str]:
        return {f"{mine}:d"}

    async def needed() -> set[str]:
        return {"nightshift-env:used"}

    async def remove(tags: list[str]) -> list[str]:
        removed.extend(tags)
        return tags

    monkeypatch.setattr(images, "images", fake_images)
    monkeypatch.setattr(images, "in_use", busy)
    monkeypatch.setattr(images, "needed_env_images", needed)
    monkeypatch.setattr(images, "_remove", remove)
    await images.prune()
    assert f"{mine}:c" in removed and f"{mine}:d" not in removed  # past the newest two, unless in use
    assert f"{mine}:a" not in removed and f"{mine}:b" not in removed
    assert "nightshift-repo/gone__repo:x" in removed and "nightshift-partial/gone__repo:k" in removed
    assert "nightshift-env:unused" in removed and "nightshift-env:used" not in removed

    use = await images.usage()
    mine_row = next(r for r in use["repos"] if r["repo"] == repo_name)
    assert (mine_row["images"], mine_row["size"]) == (4, 12_000_000_000) and use["keep"] == 2
