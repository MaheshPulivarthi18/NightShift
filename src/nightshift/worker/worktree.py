"""Per-item worktrees: each task's change as a git checkout on your disk.

A sandbox is thrown away after every run, and the change lives in the database. The worktree keeps it where
you can open it in your editor, read it, or change the fix by hand. One blobless bare clone per repo is shared
by all its tasks (`<worktrees_dir>/clones/owner__name.git`); each task has a worktree beside it
(`<worktrees_dir>/worktrees/owner__name/task-<id>`) at the task's base commit, holding its change.

After a run NightShift writes the change into the worktree and keeps a fingerprint of what it wrote. If the
worktree differs from that later, you edited it: the task says so, Publish waits until your edits are checked,
and the next run starts from them. The worktree is never mounted into a sandbox, so repo code still runs only
there; on the host git only checks out and applies patches, which runs nothing from the repo. Archiving a task
removes its worktree. A worktree that cannot be made (no network) never fails a run: it is skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import structlog

from nightshift.config import get_settings
from nightshift.worker.verify import EXCLUDES

log = structlog.get_logger()


class WorktreeError(RuntimeError):
    pass


def remote_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def _root() -> Path:
    return Path(get_settings().worktrees_dir).expanduser()


def _slug(full_name: str) -> str:
    return full_name.lower().replace("/", "__")


def clone_path(full_name: str) -> Path:
    return _root() / "clones" / f"{_slug(full_name)}.git"


def path_for(full_name: str, task_id: int) -> Path:
    return _root() / "worktrees" / _slug(full_name) / f"task-{task_id}"


def fingerprint(patch: str) -> str:
    return hashlib.sha256(patch.encode()).hexdigest()[:16]


async def _git(*args: str, cwd: Path | None = None, timeout: float = 600) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # never wait for a password
    )  # fmt: skip
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as e:
        proc.kill()
        raise WorktreeError(f"git {args[0]} took over {timeout:.0f} s") from e
    if proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args[:2])} failed: {err.decode(errors='replace').strip()[:300]}")
    return out.decode(errors="replace")


async def _has(clone: Path, commit: str) -> bool:
    try:
        await _git("cat-file", "-e", f"{commit}^{{commit}}", cwd=clone)
        return True
    except WorktreeError:
        return False


async def _clone(full_name: str) -> Path:
    """The repo's shared bare clone, made once; blobless over https, so files arrive as they are checked out."""
    clone = clone_path(full_name)
    if not (clone / "HEAD").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        url = remote_url(full_name)
        lazy = ["--filter=blob:none"] if url.startswith("https://") else []
        await _git("clone", "--bare", "--quiet", *lazy, url, str(clone))
        # build and test output never counts as an edit (the same list the sandbox's diffs leave out)
        (clone / "info").mkdir(exist_ok=True)
        (clone / "info" / "exclude").write_text("\n".join(EXCLUDES) + "\n")
    return clone


async def _fetch(clone: Path, commit: str) -> None:
    if await _has(clone, commit):
        return
    await _git("fetch", "--quiet", "origin", cwd=clone)
    if not await _has(clone, commit):  # not on a branch any more (a PR's base): ask for the commit itself
        await _git("fetch", "--quiet", "origin", commit, cwd=clone)


async def ensure(full_name: str, task_id: int, base: str) -> Path:
    """The task's worktree, made at `base` if it does not exist yet."""
    path = path_for(full_name, task_id)
    if (path / ".git").exists():
        return path
    clone = await _clone(full_name)
    await _fetch(clone, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    await _git("worktree", "prune", cwd=clone)
    await _git("worktree", "add", "--detach", "--quiet", str(path), base, cwd=clone)
    return path


async def current(path: Path) -> str:
    """The worktree's change against its own commit, new files included."""
    await _git("add", "--all", "--intent-to-add", cwd=path)
    return await _git("diff", "HEAD", cwd=path)


async def write(full_name: str, path: Path, base: str, patch: str) -> str:
    """Make the worktree hold exactly `patch` on `base`; returns the fingerprint to compare later edits with."""
    await _fetch(clone_path(full_name), base)
    await _git("reset", "--quiet", cwd=path)
    await _git("checkout", "--quiet", "--detach", "--force", base, cwd=path)
    await _git("clean", "-fdq", cwd=path)
    if patch.strip():
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
            f.write(patch if patch.endswith("\n") else patch + "\n")
        try:
            await _git("apply", "--whitespace=nowarn", f.name, cwd=path)
        finally:
            Path(f.name).unlink(missing_ok=True)
    return fingerprint(await current(path))


async def remove(full_name: str, task_id: int) -> None:
    path = path_for(full_name, task_id)
    clone = clone_path(full_name)
    if (clone / "HEAD").exists():
        with contextlib.suppress(WorktreeError):
            await _git("worktree", "remove", "--force", str(path), cwd=clone)
        await _git("worktree", "prune", cwd=clone)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


async def edited(path: Path, written: str | None) -> bool:
    """Whether the worktree changed since NightShift last wrote it."""
    if not written or not (path / ".git").exists():
        return False
    return fingerprint(await current(path)) != written
