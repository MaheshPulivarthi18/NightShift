"""Sandbox images on disk: what each repo keeps, and cleaning up what nothing needs.

Every setup on a new commit with changed dependency files saves a repo image, so a busy repo collects them (a
20-PR benchmark left 13 of 3 to 6 GB for one repo). After each save only the newest few per repo are kept;
`prune` also drops environment images no repo's environment names any more, and setup checkpoints. An image a
running sandbox uses is never removed. Sizes are each image's unique size: what deleting it frees (images
share their base layers, so their full sizes add up to several times the disk they use).
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog
from sqlalchemy import select

from nightshift.config import get_settings
from nightshift.db import Repo, RepoContext, session_scope
from nightshift.sandbox.docker import _docker, checkpoint_name, repo_image_name

log = structlog.get_logger()

PREFIXES = ("nightshift-repo/", "nightshift-partial/", "nightshift-env")
_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def size_bytes(text: str) -> int:
    """Docker's "3.03GB" as bytes."""
    m = re.match(r"([\d.]+)\s*([KMGT]?B)", text.strip(), re.I)
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()]) if m else 0


async def images() -> list[dict[str, Any]]:
    """NightShift's images, newest first: {"tag", "id", "size" (unique: freed by deleting it), "created"}."""
    res = await _docker("system", "df", "-v", "--format", "{{json .}}", timeout=180)
    try:
        rows = json.loads(res.stdout or "{}").get("Images") or []
    except ValueError:
        rows = []
    out = []
    for row in rows:
        if row.get("Tag") == "<none>" or not str(row.get("Repository", "")).startswith(PREFIXES):
            continue
        out.append({"tag": f"{row['Repository']}:{row['Tag']}", "id": row.get("ID", ""),
                    "size": size_bytes(row.get("UniqueSize", "")), "created": row.get("CreatedAt", "")})  # fmt: skip
    return sorted(out, key=lambda i: i["created"], reverse=True)


async def in_use() -> set[str]:
    """Images of running or stopped containers: a sandbox may still need them."""
    res = await _docker("ps", "-a", "--format", "{{.Image}}", timeout=30)
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


def _stale(tags: list[str], keep: int, busy: set[str]) -> list[str]:
    """Of a repo's images, newest first, the ones past the newest `keep` that no container uses."""
    return [t for t in tags[keep:] if t not in busy]


async def _remove(tags: list[str]) -> list[str]:
    removed = []
    for tag in tags:
        if (await _docker("rmi", tag, timeout=120)).ok:  # no -f: an image something still needs stays
            removed.append(tag)
    if removed:
        log.info("sandbox_images_removed", images=removed)
    return removed


async def prune_repo(full_name: str, keep: int | None = None) -> list[str]:
    """Keep the newest `keep` images of one repo (repo_images_kept by default)."""
    keep = get_settings().repo_images_kept if keep is None else keep
    prefix = repo_image_name(full_name) + ":"
    tags = [i["tag"] for i in await images() if i["tag"].startswith(prefix)]
    return await _remove(_stale(tags, keep, await in_use()))


async def needed_env_images() -> set[str]:
    """Environment images a repo's environment names (older environments too: a task may still use one)."""
    async with session_scope() as s:
        rows = (await s.execute(select(RepoContext).where(RepoContext.kind == "environment"))).scalars().all()
    return {img for r in rows if (img := (r.data or {}).get("image"))}


async def usage() -> dict[str, Any]:
    """Images per repo and the rest, for the Settings page."""
    all_images = await images()
    async with session_scope() as s:
        repos = (await s.execute(select(Repo.full_name))).scalars().all()
    by_prefix = {repo_image_name(r): r for r in repos}
    per: dict[str, dict[str, Any]] = {}
    other = {"images": 0, "size": 0}
    for i in all_images:
        repo = by_prefix.get(i["tag"].split(":", 1)[0])
        if repo is None:
            other["images"] += 1
            other["size"] += i["size"]
            continue
        row = per.setdefault(repo, {"repo": repo, "images": 0, "size": 0, "newest": i["size"]})
        row["images"] += 1
        row["size"] += i["size"]
    keep = get_settings().repo_images_kept
    return {
        "repos": sorted(per.values(), key=lambda r: -r["size"]),
        "other": other,
        "total": sum(i["size"] for i in all_images),
        "keep": keep,
        "removable": sum(max(r["images"] - keep, 0) for r in per.values()),
    }


async def prune() -> list[str]:
    """Clean up: each repo down to its newest few images, its setup checkpoints, environment images no
    environment names, and images of repos no longer in the database."""
    settings = get_settings()
    all_images = await images()
    busy = await in_use()
    async with session_scope() as s:
        repos = set((await s.execute(select(Repo.full_name))).scalars().all())
    known = {repo_image_name(r) for r in repos}
    gone: list[str] = []
    by_repo: dict[str, list[str]] = {}
    for i in all_images:
        by_repo.setdefault(i["tag"].split(":", 1)[0], []).append(i["tag"])
    for prefix, tags in by_repo.items():
        if prefix.startswith("nightshift-repo/"):
            gone += _stale(tags, settings.repo_images_kept if prefix in known else 0, busy)
        elif prefix.startswith("nightshift-partial/") and prefix not in {checkpoint_name(r) for r in repos}:
            gone += [t for t in tags if t not in busy]
    needed = await needed_env_images()
    gone += [i["tag"] for i in all_images if i["tag"].startswith("nightshift-env:") and i["tag"] not in needed
             and i["tag"] not in busy]  # fmt: skip
    return await _remove(gone)


async def remove_repo(full_name: str) -> list[str]:
    """Every image of a repo that is being purged."""
    prefixes = (repo_image_name(full_name) + ":", checkpoint_name(full_name) + ":")
    busy = await in_use()
    return await _remove([i["tag"] for i in await images() if i["tag"].startswith(prefixes) and i["tag"] not in busy])
