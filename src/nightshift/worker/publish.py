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
import os
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
    """Run git or gh as the operator (their gh login and SSH keys), never with NightShift's token."""
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), 600)
    if check and proc.returncode != 0:
        raise PublishError(f"`{' '.join(args[:4])}` failed: {(err or out).decode(errors='replace')[-800:]}")
    return out.decode(errors="replace")


def attribution_for(repo: Repo) -> str:
    """The commit's last line for this repo: what Gate 2 remembered for it, else the global setting."""
    return str((repo.publish or {}).get("attribution", get_settings().commit_attribution))


def branch_name(task: Task, repo: Repo | None = None) -> str:
    prefix = (repo.publish or {}).get("branch_prefix", get_settings().branch_prefix) if repo else None
    prefix = get_settings().branch_prefix if prefix is None else prefix
    slug = re.sub(r"[^a-z0-9]+", "-", str(task.issue.get("title", "")).lower()).strip("-")[:40].strip("-")
    name = f"{task.issue_number}-{slug or 'fix'}"
    return f"{prefix}/{name}" if prefix else name


BRANCH_RE = re.compile(r"^(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,120}(?<![/.])$")


def with_attribution(message: str, attribution: str) -> str:
    message = message.strip()
    return f"{message}\n\n{attribution.strip()}\n" if attribution.strip() else message + "\n"


def strip_attribution(message: str) -> str:
    """A commit message without the attribution line older drafts ended with."""
    line = get_settings().commit_attribution.strip()
    text = message.rstrip()
    if line and text.endswith(line):
        text = text[: -len(line)].rstrip()
    return text


def template_headings(template: str) -> list[str]:
    """The section headings of a PR template, e.g. ["Description", "Testing"]."""
    return [m.strip().strip("#").strip() for m in re.findall(r"^#{1,6}\s+(.+?)\s*$", template, re.M)]


def missing_sections(body: str, headings: list[str]) -> list[str]:
    present = {h.strip().lower() for h in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", body, re.M)}
    return [h for h in headings if h.lower() not in present]


async def freshness(full_name: str, task: Task) -> list[str]:
    """Reasons it is no longer safe to publish; empty when the issue is still ours to fix."""
    if task.rehearsal:
        return ["this is a rehearsal: it re-solved an issue that already has a fix"]
    issue = await fetch_issue(full_name, task.issue_number)
    return issue.blockers(get_settings().github_login or None)


async def prepare(repo: Repo, task: Task, workdir: Path, url: str | None = None, branch: str | None = None) -> Prepared:
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
    branch = branch or branch_name(task, repo)
    if not BRANCH_RE.match(branch):
        raise PublishError(f"Not a usable branch name: {branch!r}")
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
    await _run("gh", "repo", "fork", repo.full_name, "--clone=false")  # no-op when the fork exists
    name = repo.full_name.split("/", 1)[1]
    fork = ""
    for _ in range(12):  # GitHub creates forks asynchronously
        fork = await _run("gh", "repo", "view", f"{login}/{name}", "--json", "sshUrl", "--jq", ".sshUrl", check=False)
        if fork.strip():
            break
        await asyncio.sleep(5)
    fork = fork.strip()
    if not fork:
        raise PublishError(f"The fork {login}/{name} did not appear on GitHub.")
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
