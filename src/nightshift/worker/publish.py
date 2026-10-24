"""Gate 2 and the publisher: the only code that writes to GitHub, run on the host with the operator's `gh` login.

Before anything public, `prepare` checks freshness right then:
  - the issue is still open and nobody else has claimed it or opened a PR for it
  - upstream has not changed the files the fix touches since the commit it was verified on
    (if it has, the fix must be re-run on the new commit: its tests proved nothing about that code)
and applies the verified diff to a fresh clone of the current default branch.

`publish` repeats the freshness check, commits as the operator, pushes to the operator's fork and opens
the PR. Nothing in the repo is executed on the host: git only clones, applies and commits.
Rehearsal tasks can never be published.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nightshift.config import get_settings
from nightshift.db import Repo, Task
from nightshift.worker.issue import fetch_issue


class PublishError(RuntimeError):
    pass


class Prepared(BaseModel):
    workdir: str
    branch: str
    default_branch: str
    head: str  # upstream commit the change is applied on
    drift: list[str]  # files the fix touches that changed upstream since the verified base
    files: list[str]


async def _run(*args: str, cwd: Path | None = None, stdin: str | None = None, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), 600)
    if check and proc.returncode != 0:
        raise PublishError(f"`{' '.join(args[:4])}` failed: {(err or out).decode(errors='replace')[-800:]}")
    return out.decode(errors="replace")


def branch_name(task: Task) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(task.issue.get("title", "")).lower()).strip("-")[:40].strip("-")
    return f"nightshift/{task.issue_number}-{slug or 'fix'}"


async def freshness(full_name: str, task: Task) -> list[str]:
    """Reasons it is no longer safe to publish; empty when the issue is still ours to fix."""
    if task.rehearsal:
        return ["this is a rehearsal: it re-solved an issue that already has a fix"]
    issue = await fetch_issue(full_name, task.issue_number)
    return issue.blockers(get_settings().github_login or None)


async def prepare(repo: Repo, task: Task, workdir: Path, url: str | None = None) -> Prepared:
    """Fresh clone of upstream (or `url`), the verified diff applied on its current default branch."""
    if not task.diff:
        raise PublishError("The task has no diff.")
    if workdir.exists():
        shutil.rmtree(workdir)
    url = url or f"https://github.com/{repo.full_name}.git"
    await _run("git", "clone", "--quiet", "--filter=blob:none", url, str(workdir))
    default = (await _run("git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=workdir)).strip()
    default = default.removeprefix("origin/")
    head = (await _run("git", "rev-parse", "HEAD", cwd=workdir)).strip()
    files = sorted(
        {m for m in re.findall(r"^\+\+\+ b/(.+)$", task.diff, re.M)}
        | {m for m in re.findall(r"^--- a/(.+)$", task.diff, re.M)}
    )
    drift: list[str] = []
    if head != task.base_commit:
        changed = await _run("git", "diff", "--name-only", task.base_commit, head, "--", *files, cwd=workdir)
        drift = [p for p in changed.splitlines() if p]
    branch = branch_name(task)
    await _run("git", "checkout", "--quiet", "-b", branch, cwd=workdir)
    await _run("git", "apply", "--index", "--whitespace=nowarn", "-", cwd=workdir, stdin=task.diff)
    return Prepared(workdir=str(workdir), branch=branch, default_branch=default, head=head, drift=drift, files=files)


async def publish(repo: Repo, task: Task, prepared: Prepared, *, title: str, body: str, message: str) -> str:
    """Commit, push to the operator's fork, open the PR. Returns the PR URL. Gate 2 must already be passed."""
    login = get_settings().github_login
    if not login:
        raise PublishError("Set NIGHTSHIFT_GITHUB_LOGIN to the account that will open the PR.")
    if blockers := await freshness(repo.full_name, task):
        raise PublishError("Not publishing: " + "; ".join(blockers))
    if prepared.drift:
        raise PublishError(
            "Upstream changed files this fix touches since it was verified: " + ", ".join(prepared.drift)
            + ". Re-run the task on the new commit."
        )  # fmt: skip
    wd = Path(prepared.workdir)
    await _run("git", "commit", "--quiet", "-F", "-", cwd=wd, stdin=message)
    await _run("gh", "repo", "fork", repo.full_name, "--clone=false", "--remote=false", check=False)
    name = repo.full_name.split("/", 1)[1]
    fork = (await _run("gh", "repo", "view", f"{login}/{name}", "--json", "sshUrl", "--jq", ".sshUrl")).strip()
    await _run("git", "push", "--quiet", fork, f"HEAD:refs/heads/{prepared.branch}", cwd=wd)
    url = await _run(
        "gh", "pr", "create", "--repo", repo.full_name, "--base", prepared.default_branch,
        "--head", f"{login}:{prepared.branch}", "--title", title, "--body-file", "-", stdin=body,
    )  # fmt: skip
    return url.strip().splitlines()[-1]


async def post_claim(repo: Repo, task: Task, text: str) -> str:
    """Post the approved claim comment on the issue (after a freshness check). Returns the comment URL."""
    if blockers := await freshness(repo.full_name, task):
        raise PublishError("Not claiming: " + "; ".join(blockers))
    out = await _run(
        "gh", "issue", "comment", str(task.issue_number), "--repo", repo.full_name, "--body-file", "-", stdin=text
    )
    return out.strip().splitlines()[-1]


def summary(prepared: Prepared, title: str, body: str) -> dict[str, Any]:
    return {"branch": prepared.branch, "base": prepared.default_branch, "head": prepared.head,
            "drift": prepared.drift, "files": prepared.files, "title": title, "body": body}  # fmt: skip
