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
import codecs
import contextlib
import hashlib
import json
import re
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from nightshift import events
from nightshift.config import get_settings

log = structlog.get_logger()

INSIDE_DIR = Path(__file__).parent / "inside"
TOOLS_MOUNT = "/opt/nightshift"
TOOLS_PYTHON = "/opt/ns-tools/bin/python"
REPO_DIR = "/work/repo"
RESULT_MARKER = "@@NIGHTSHIFT_RESULT@@"
BASE_LABEL = "nightshift.base"  # on a repo image: the environment image it was built on
REPO_TOOLS_PATH = f'export PATH="{REPO_DIR}/.venv/bin:{REPO_DIR}/node_modules/.bin:$PATH";'
OUTPUT_LIMIT = 200_000  # characters kept from each end of a command's output
# A shown command's output reaches the log while it runs: new output every FLUSH_SECONDS, each piece cut to
# its last LIVE_CHARS, at most LIVE_PIECES pieces per command (an install can print for many minutes).
FLUSH_SECONDS = 2.0
LIVE_CHARS = 4000
LIVE_PIECES = 150
TAIL_CHARS = 4000  # kept with the finished command

# Files whose contents decide what a repo's setup installs. A change to any of them means a new image.
DEP_FILES = {
    "requirements.txt", "requirements.lock", "requirements-dev.txt", "requirements-dev.lock",
    "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile.lock", "setup.py", "setup.cfg",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
}  # fmt: skip


# Failures worth one retry: the network, not the repo.
NETWORK_ERROR = re.compile(
    r"operation timed out|timed out|Request failed after \d+ retries|ECONNRESET|ETIMEDOUT|EAI_AGAIN"
    r"|Temporary failure in name resolution|Connection reset|Read timed out|503 Service Unavailable",
    re.IGNORECASE,
)


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


async def _docker_streamed(
    *args: str, timeout: float, on_output: Callable[[str], Awaitable[None]], stdin: bytes | None = None
) -> ExecResult:
    """Like _docker, but hands new output to `on_output` every FLUSH_SECONDS while the command runs."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "docker", *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err, pending = bytearray(), bytearray(), []

    async def feed_stdin() -> None:
        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()

    async def pump(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
        decode = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while stream is not None and (chunk := await stream.read(65536)):
            buf += chunk
            pending.append(decode.decode(chunk))

    async def flush() -> None:
        if pending:
            text = "".join(pending)
            pending.clear()
            await on_output(text)

    async def flusher() -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            await flush()

    feeder = asyncio.create_task(flusher())
    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.gather(feed_stdin(), pump(proc.stdout, out), pump(proc.stderr, err), proc.wait()), timeout
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        timed_out = True
    finally:
        feeder.cancel()
    await flush()
    if timed_out:
        return ExecResult(-1, "", f"docker {args[0]} timed out after {timeout:.0f}s", time.monotonic() - started, True)
    return ExecResult(
        proc.returncode or 0,
        _clip(out.decode(errors="replace")),
        _clip(err.decode(errors="replace")),
        time.monotonic() - started,
        False,
    )


async def docker_shown(*args: str, timeout: float, command: str, note: str, stdin: bytes | None = None) -> ExecResult:
    """Run docker; with a run under way, `command` (what the log calls it), its output as it comes and its
    result go to that run's log. Without one it is a plain _docker call."""
    run_id = events.current_run()
    if run_id is None:
        return await _docker(*args, timeout=timeout, stdin=stdin)
    cmd = uuid.uuid4().hex[:12]
    pieces = 0
    await events.record(run_id, events.CMD_START, command, cmd=cmd, note=note, timeout=int(timeout))

    async def live(text: str) -> None:
        nonlocal pieces
        pieces += 1
        if pieces > LIVE_PIECES:
            return
        if pieces == LIVE_PIECES:
            text = "[live output stops here; the end of the output shows when the command finishes]\n"
        elif len(text) > LIVE_CHARS:
            text = "[...]\n" + text[-LIVE_CHARS:]
        await events.record(run_id, events.CMD_OUTPUT, events.redact(text), cmd=cmd)

    res = await _docker_streamed(*args, timeout=timeout, on_output=live, stdin=stdin)
    await events.record(
        run_id, events.CMD_END, command, cmd=cmd, exit=res.code, seconds=round(res.seconds, 1),
        timed_out=res.timed_out or res.code == 124, tail=events.redact(res.tail(TAIL_CHARS)),
    )  # fmt: skip
    return res


def repo_image_name(full_name: str) -> str:
    return "nightshift-repo/" + full_name.lower().replace("/", "__")


# A setup that fails part way keeps what it got: after each slow command that succeeded, the sandbox is saved
# here, and the next setup that starts with the same commands resumes from it instead of running them again.
CHECKPOINT_SECONDS = 20  # a command faster than this is cheaper to run again than to save
CHECKPOINTS_KEPT = 2  # per repo; a setup that succeeds removes them all
LOG_PATH = re.compile(r"(?<![\w.-])(/[\w.@+-]+(?:/[\w.@+-]+)*\.(?:log|txt))\b")
LOG_LINES = 60


def checkpoint_name(full_name: str) -> str:
    return "nightshift-partial/" + full_name.lower().replace("/", "__")


def setup_key(base: str, sums: str, commands: list[str]) -> str:
    """The image key for a setup: the base image, the dependency files' hashes and the commands run."""
    return hashlib.sha256((base + sums + "\n".join(commands)).encode()).hexdigest()[:12]


async def _checkpoint(box: Sandbox, full_name: str, key: str, base: str) -> None:
    """Save the sandbox after a slow command that succeeded; keep only the newest few for the repo."""
    tag = f"{checkpoint_name(full_name)}:{key}"
    try:
        await box.commit(tag, base)
    except SandboxError as e:  # a checkpoint is an optimisation: the setup goes on without it
        log.warning("setup_checkpoint_not_saved", repo=full_name, error=str(e))
        return
    res = await _docker("images", checkpoint_name(full_name), "--format", "{{.Repository}}:{{.Tag}}", timeout=30)
    for old in [t for t in res.stdout.splitlines() if t and not t.endswith(":<none>")][CHECKPOINTS_KEPT:]:
        await _docker("rmi", "-f", old, timeout=60)


async def _drop_checkpoints(full_name: str) -> None:
    res = await _docker("images", checkpoint_name(full_name), "--format", "{{.Repository}}:{{.Tag}}", timeout=30)
    for tag in [t for t in res.stdout.splitlines() if t and not t.endswith(":<none>")]:
        # -f: the sandbox that just finished may still run from it; only the name goes, and the repo image
        # built on top of it keeps the layers it needs
        await _docker("rmi", "-f", tag, timeout=60)


async def _log_tails(box: Sandbox, output: str) -> str:
    """The end of each log file a failed command's output names ("see /tmp/build.log"): often the real error
    is only there. At most two files."""
    out = []
    for path in list(dict.fromkeys(LOG_PATH.findall(output)))[:2]:
        res = await box.exec(f"tail -n {LOG_LINES} -- {shlex.quote(path)}", workdir="/")
        if res.ok and res.stdout.strip():
            out.append(f"\n--- the end of {path} ---\n{res.stdout.strip()[-3000:]}")
    return "".join(out)


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
            # Package downloads: patient timeouts and retries (no secrets; these are plain tool settings).
            "--env", "UV_HTTP_TIMEOUT=120", "--env", "UV_HTTP_RETRIES=6",
            "--env", "PIP_DEFAULT_TIMEOUT=120", "--env", "npm_config_fetch_retries=5",
            # A repo image from an earlier run already has a .venv, and `uv venv` refuses to overwrite one
            # without this; the packages come back from uv's cache in the image.
            "--env", "UV_VENV_CLEAR=1",
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

    async def exec(
        self,
        command: str,
        *,
        workdir: str = REPO_DIR,
        timeout: float | None = None,
        show: str | None = None,
        note: str = "",
    ) -> ExecResult:
        """Run a shell command inside the container. The timeout is enforced inside too, so a
        command that outlives it is killed rather than left running in the background.

        `show`: the command as the log shows it (without NightShift's own wrapping); with a run under way,
        the command, its output as it comes and its result go to that run's log. `note` says why it ran
        ("baseline", "targeted test, without the fix"). Housekeeping (git, file reads) is not shown."""
        limit = timeout or get_settings().sandbox_command_timeout
        # pipefail: `pytest | tail` must fail when pytest does. The repo's own virtualenv and node tools come
        # first on PATH (set after the login shell, which resets it), so `pytest` or `jest` is the repo's.
        script = f"{REPO_TOOLS_PATH} set -o pipefail; {command}"
        inner = f"timeout --kill-after=10 {int(limit)} bash -lc {shlex.quote(script)}"
        args = ("exec", "--workdir", workdir, self.name, "bash", "-c", inner)
        if show is None:
            res = await _docker(*args, timeout=limit + 30)
        else:
            res = await docker_shown(*args, timeout=limit + 30, command=show or command, note=note)
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

    async def commit(self, tag: str, base: str) -> None:
        res = await _docker(
            "commit", "--change", 'CMD ["sleep", "infinity"]', "--change", f"LABEL {BASE_LABEL}={base}",
            self.name, tag, timeout=600,
        )  # fmt: skip
        if not res.ok:
            raise SandboxError(f"Could not save image {tag}: {res.tail(500)}")

    async def remove(self) -> None:
        await _docker("rm", "--force", self.name, timeout=60)
        log.info("sandbox_removed", name=self.name)


async def remove_orphans(older_than: float) -> list[str]:
    """Remove task sandboxes left running by runs that died: containers labelled nightshift=task that
    are older than `older_than` seconds, longer than any run may take. Returns their names."""
    res = await _docker(
        "ps", "--all", "--filter", "label=nightshift=task", "--format", "{{.Names}}\t{{.CreatedAt}}", timeout=30
    )
    if not res.ok:
        return []
    old = []
    for line in res.stdout.splitlines():
        name, _, created = line.partition("\t")
        try:  # "2026-10-25 09:39:44 +0530 IST"
            started = datetime.strptime(" ".join(created.split()[:3]), "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            continue
        if (datetime.now(UTC) - started).total_seconds() > older_than:
            old.append(name)
    for name in old:
        await _docker("rm", "--force", name, timeout=60)
        log.warning("sandbox_orphan_removed", name=name)
    return old


async def image_exists(tag: str) -> bool:
    return (await _docker("image", "inspect", tag, timeout=30)).ok


async def newest_repo_image(full_name: str, base: str) -> str | None:
    """The most recently built image of this repo on `base` (docker lists images newest first), or None.

    Images saved before environments existed carry no label; they were all built on the default base."""
    res = await _docker("images", repo_image_name(full_name), "--format", "{{.Repository}}:{{.Tag}}", timeout=30)
    tags = [t for t in res.stdout.splitlines() if t and not t.endswith(":<none>")]
    if not res.ok or not tags:
        return None
    labels = await _docker(
        "image", "inspect", "--format", f'{{{{index .Config.Labels "{BASE_LABEL}"}}}}', *tags, timeout=30
    )
    if not labels.ok:
        return None
    default = get_settings().sandbox_image
    for tag, built_on in zip(tags, labels.stdout.splitlines(), strict=False):
        if (built_on.strip().replace("<no value>", "") or default) == base:
            return tag
    return None


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
    image: str | None = None,
) -> tuple[Sandbox, bool]:
    """A running sandbox with `full_name` checked out at `ref` and its setup commands done.

    Returns (sandbox, from_cache). `fetch_url` fetches `ref` from another remote first (a fork).
    `image` is the environment to start from (see envimage); the default is the base image.
    The caller owns the sandbox and must remove it.
    """
    url = f"https://github.com/{full_name}.git"
    base = image or get_settings().sandbox_image
    box = await Sandbox.start(base)
    try:
        await _must(box, f"git clone --quiet --filter=blob:none {url} {REPO_DIR}", "clone", 600, "/work")
        await _checkout(box, ref, fetch_url)
        files = [p for p in (await _must(box, "git ls-files", "list files")).stdout.splitlines()
                 if p.rsplit("/", 1)[-1] in DEP_FILES]  # fmt: skip
        sums = (await _must(box, "sha256sum " + " ".join(map(shlex.quote, files)), "hash deps")).stdout if files else ""
        # The base image is part of the key: a repo image built on another base is never reused.
        tag = f"{repo_image_name(full_name)}:{setup_key(base, sums, setup)}"

        if await image_exists(tag):
            await box.remove()
            box = await Sandbox.start(tag)
            await _must(box, "git fetch --quiet --filter=blob:none origin", "fetch")
            await _checkout(box, ref, fetch_url)
            log.info("sandbox_from_cache", repo=full_name, image=tag)
            return box, True

        # No exact match: start from this repo's newest image, if any, and run the setup on top of it, so
        # only packages that changed are downloaded. Setup commands must tolerate an existing install.
        done = 0  # setup commands already run, in a checkpoint this setup resumes from
        for k in range(len(setup) - 1, 0, -1):
            saved = f"{checkpoint_name(full_name)}:{setup_key(base, sums, setup[:k])}"
            if await image_exists(saved):
                await box.remove()
                box = await Sandbox.start(saved)
                await _must(box, "git fetch --quiet --filter=blob:none origin", "fetch")
                await _checkout(box, ref, fetch_url)
                done = k
                await events.record(
                    events.current_run(), events.FIXED,
                    f"Setup resumed from a checkpoint: {k} of {len(setup)} commands already done",
                )  # fmt: skip
                break
        if not done and (previous := await newest_repo_image(full_name, base)):
            await box.remove()
            box = await Sandbox.start(previous)
            await _must(box, "git fetch --quiet --filter=blob:none origin", "fetch")
            await _checkout(box, ref, fetch_url)
            log.info("sandbox_from_previous_image", repo=full_name, image=previous)

        for i, command in enumerate(setup[done:], start=done):
            log.info("sandbox_setup", repo=full_name, command=command)
            res = await box.exec(command, show=command, note="setup")
            if not res.ok and not res.timed_out and NETWORK_ERROR.search(res.tail(4000)):
                log.warning("sandbox_setup_network_retry", repo=full_name, command=command)
                # a download that timed out usually works the second time
                res = await box.exec(command, show=command, note="setup, again after a network error")
            if not res.ok:
                logs = await _log_tails(box, res.tail(8000))
                raise SandboxError(f"setup command `{command}` failed (exit {res.code}):\n{res.tail()}{logs}")
            if res.seconds >= CHECKPOINT_SECONDS and i < len(setup) - 1:
                await _checkpoint(box, full_name, setup_key(base, sums, setup[: i + 1]), base)
        await box.commit(tag, base)
        await _drop_checkpoints(full_name)  # this setup is saved whole now
        log.info("sandbox_image_saved", repo=full_name, image=tag)
        from nightshift.sandbox.images import prune_repo  # it imports this module

        with contextlib.suppress(Exception):  # disk hygiene never fails a setup
            await prune_repo(full_name)
        return box, False
    except BaseException:
        await box.remove()
        raise
