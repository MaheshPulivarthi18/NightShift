"""Per-task Docker sandboxes: the only place a repo's code is installed, built or run.

The host keeps the brain (agents, model calls, secrets); a container is the hands. A container gets
no environment variables, no credentials and no host files except NightShift's own context scripts,
mounted read-only. It runs as a non-root user with no Linux capabilities, under CPU, memory and
process caps, and every command has a timeout enforced inside the container.

After a repo's setup commands succeed, the container is committed as a per-repo image keyed by the
repo's dependency files and setup commands, so the next task in that repo skips the install.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from nightshift.config import get_settings

log = structlog.get_logger()

INSIDE_DIR = Path(__file__).parent / "inside"
TOOLS_MOUNT = "/opt/nightshift"
TOOLS_PYTHON = "/opt/ns-tools/bin/python"
REPO_DIR = "/work/repo"
RESULT_MARKER = "@@NIGHTSHIFT_RESULT@@"
OUTPUT_LIMIT = 200_000  # characters kept from each end of a command's output

# Files whose contents decide what a repo's setup installs. A change to any of them means a new image.
DEP_FILES = {
    "requirements.txt", "requirements.lock", "requirements-dev.txt", "requirements-dev.lock",
    "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile.lock", "setup.py", "setup.cfg",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
}  # fmt: skip


class SandboxError(RuntimeError):
    pass


@dataclass
class ExecResult:
    code: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def tail(self, chars: int = 3000) -> str:
        return (self.stdout + self.stderr)[-chars:]


def _clip(text: str) -> str:
    if len(text) <= 2 * OUTPUT_LIMIT:
        return text
    return text[:OUTPUT_LIMIT] + "\n[... output trimmed ...]\n" + text[-OUTPUT_LIMIT:]


async def _docker(*args: str, timeout: float = 120, stdin: bytes | None = None) -> ExecResult:
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ExecResult(-1, "", f"docker {args[0]} timed out after {timeout:.0f}s", time.monotonic() - started, True)
    return ExecResult(
        proc.returncode or 0,
        _clip(out.decode(errors="replace")),
        _clip(err.decode(errors="replace")),
        time.monotonic() - started,
        False,
    )


def repo_image_name(full_name: str) -> str:
    return "nightshift-repo/" + full_name.lower().replace("/", "__")


class Sandbox:
    """One running container. Use `async with await Sandbox.start(...)` so it is always removed."""

    def __init__(self, name: str, image: str) -> None:
        self.name, self.image = name, image

    @classmethod
    async def start(cls, image: str | None = None) -> Sandbox:
        settings = get_settings()
        image = image or settings.sandbox_image
        name = f"ns-task-{uuid.uuid4().hex[:10]}"
        res = await _docker(
            "run", "--detach", "--name", name, "--label", "nightshift=task",
            "--cpus", str(settings.sandbox_cpus), "--memory", settings.sandbox_memory,
            "--pids-limit", str(settings.sandbox_pids),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--volume", f"{INSIDE_DIR}:{TOOLS_MOUNT}:ro",
            image, "sleep", "infinity",
        )  # fmt: skip
        if not res.ok:
            raise SandboxError(f"Could not start a sandbox from {image}: {res.tail(800)}")
        log.info("sandbox_started", name=name, image=image)
        return cls(name, image)

    async def __aenter__(self) -> Sandbox:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.remove()

    async def exec(self, command: str, *, workdir: str = REPO_DIR, timeout: float | None = None) -> ExecResult:
        """Run a shell command inside the container. The timeout is enforced inside too, so a
        command that outlives it is killed rather than left running in the background."""
        limit = timeout or get_settings().sandbox_command_timeout
        # pipefail: `pytest | tail` must fail when pytest does.
        inner = f"timeout --kill-after=10 {int(limit)} bash -lc {shlex.quote('set -o pipefail; ' + command)}"
        res = await _docker("exec", "--workdir", workdir, self.name, "bash", "-c", inner, timeout=limit + 30)
        if res.code == 124:
            res.timed_out = True
        return res

    async def run_tool(self, script: str, *args: str, timeout: float = 600) -> dict[str, Any]:
        """Run one of NightShift's own scripts from `inside/` and return its JSON result.

        The result came out of a container that holds untrusted code, so callers validate it."""
        command = " ".join(shlex.quote(a) for a in (TOOLS_PYTHON, "-I", f"{TOOLS_MOUNT}/{script}", *args))
        res = await self.exec(command, timeout=timeout)
        _, marker, payload = res.stdout.rpartition(RESULT_MARKER)
        if not res.ok or not marker:
            raise SandboxError(f"{script} failed (exit {res.code}):\n{res.tail()}")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise SandboxError(f"{script} returned {type(data).__name__}, not an object")
        return data

    async def write_file(self, path: str, content: str) -> None:
        res = await _docker(
            "exec", "-i", self.name, "bash", "-c", f"cat > {shlex.quote(path)}", stdin=content.encode()
        )  # fmt: skip
        if not res.ok:
            raise SandboxError(f"Could not write {path}: {res.tail(500)}")

    async def commit(self, tag: str) -> None:
        res = await _docker("commit", "--change", 'CMD ["sleep", "infinity"]', self.name, tag, timeout=600)
        if not res.ok:
            raise SandboxError(f"Could not save image {tag}: {res.tail(500)}")

    async def remove(self) -> None:
        await _docker("rm", "--force", self.name, timeout=60)
        log.info("sandbox_removed", name=self.name)


async def image_exists(tag: str) -> bool:
    return (await _docker("image", "inspect", tag, timeout=30)).ok


async def _must(
    box: Sandbox, command: str, what: str, timeout: float | None = None, workdir: str = REPO_DIR
) -> ExecResult:
    res = await box.exec(command, timeout=timeout, workdir=workdir)
    if not res.ok:
        raise SandboxError(f"{what} failed (exit {res.code}):\n{res.tail()}")
    return res


async def _checkout(box: Sandbox, ref: str, fetch_url: str | None) -> None:
    """Check out `ref` (a commit, short or full, or a branch). With `fetch_url`, its branches are
    fetched first as `extra/<branch>`, so a fork's branch or commit can be checked out."""
    candidates = [ref]
    if fetch_url:
        refspec = "+refs/heads/*:refs/remotes/extra/*"
        await _must(box, f"git fetch --quiet --filter=blob:none {shlex.quote(fetch_url)} '{refspec}'", "fetch")
        candidates.append(f"extra/{ref}")
    attempts = " || ".join(f"git checkout --quiet --force --detach {shlex.quote(c)}" for c in candidates)
    await _must(box, attempts, f"checkout {ref}")


async def prepare_repo(
    full_name: str,
    ref: str,
    setup: list[str],
    *,
    fetch_url: str | None = None,
) -> tuple[Sandbox, bool]:
    """A running sandbox with `full_name` checked out at `ref` and its setup commands done.

    Returns (sandbox, from_cache). `fetch_url` fetches `ref` from another remote first (a fork).
    The caller owns the sandbox and must remove it.
    """
    url = f"https://github.com/{full_name}.git"
    box = await Sandbox.start()
    try:
        await _must(box, f"git clone --quiet --filter=blob:none {url} {REPO_DIR}", "clone", 600, "/work")
        await _checkout(box, ref, fetch_url)
        files = [p for p in (await _must(box, "git ls-files", "list files")).stdout.splitlines()
                 if p.rsplit("/", 1)[-1] in DEP_FILES]  # fmt: skip
        sums = (await _must(box, "sha256sum " + " ".join(map(shlex.quote, files)), "hash deps")).stdout if files else ""
        key = hashlib.sha256((sums + "\n".join(setup)).encode()).hexdigest()[:12]
        tag = f"{repo_image_name(full_name)}:{key}"

        if await image_exists(tag):
            await box.remove()
            box = await Sandbox.start(tag)
            await _must(box, "git fetch --quiet --filter=blob:none origin", "fetch")
            await _checkout(box, ref, fetch_url)
            log.info("sandbox_from_cache", repo=full_name, image=tag)
            return box, True

        for command in setup:
            log.info("sandbox_setup", repo=full_name, command=command)
            await _must(box, command, f"setup command `{command}`")
        await box.commit(tag)
        log.info("sandbox_image_saved", repo=full_name, image=tag)
        return box, False
    except BaseException:
        await box.remove()
        raise
