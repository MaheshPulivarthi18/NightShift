"""Each task's change as a git worktree on disk: written after a run, edits by hand noticed, removed when the
task is archived. Uses a local origin repo; nothing reaches GitHub."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift.db import Task
from nightshift.worker import task as worker
from nightshift.worker import worktree

pytestmark = pytest.mark.real_worktrees
PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    repo = tmp_path / "origin"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "app.py").write_text("x = 1\n")
    run("add", ".")
    run("commit", "-qm", "first")
    base = run("rev-parse", "HEAD").strip()
    monkeypatch.setattr(worktree, "remote_url", lambda _: str(repo))
    return "o/r", base


async def test_the_change_is_written_and_hand_edits_are_noticed(origin: tuple[str, str]) -> None:
    name, base = origin
    path = await worktree.ensure(name, 7, base)
    assert (path / "app.py").read_text() == "x = 1\n" and path == worktree.path_for(name, 7)
    written = await worktree.write(name, path, base, PATCH)
    assert (path / "app.py").read_text() == "x = 2\n"
    assert not await worktree.edited(path, written)

    (path / "app.py").write_text("x = 3\n")  # the developer edits the fix
    (path / "notes.py").write_text("y = 1\n")  # and adds a file
    assert await worktree.edited(path, written)
    mine = await worktree.current(path)
    assert "+x = 3" in mine and "notes.py" in mine

    task = Task(id=7, result={"worktree": {"written": written}})
    assert await worker.handmade(name, task) == mine  # where the next run starts
    assert await worker.handmade(name, Task(id=7, result={})) == ""  # never written: nothing to compare

    again = await worktree.write(name, path, base, PATCH)  # a run writes its own version back
    assert again == written and not (path / "notes.py").exists()
    await worktree.remove(name, 7)
    assert not path.exists()


async def test_keeping_the_change_never_fails_a_run(origin: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    name, base = origin
    result: dict[str, object] = {}
    await worker.keep_in_worktree(name, Task(id=8, base_commit=base), PATCH, result)
    assert result["worktree"]["path"].endswith("task-8")  # type: ignore[index]
    monkeypatch.setattr(worktree, "remote_url", lambda _: "/nowhere")
    other: dict[str, object] = {}
    await worker.keep_in_worktree("o/missing", Task(id=9, base_commit=base), PATCH, other)
    assert other == {}  # no clone: skipped, the run goes on
