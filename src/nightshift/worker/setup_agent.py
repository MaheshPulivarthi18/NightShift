"""The Setup agent: works out how to install and test a repo, and proves it in the sandbox.

It reads what the repo says about itself (CI workflows first, since they run the tests on every push;
then dev containers, contributing guides, the README, Dockerfiles and toolchain files), proposes an
environment (system packages, tools, setup and test commands), tries it in a fresh sandbox, reads what
went wrong, and tries again. The result is saved per repo, and every task there plans and runs on it.

    ensure_environment:  the repo's saved environment, or a new one found by the agent
    setup_repo:          the same as its own job (Repos page, `nightshift sandbox setup`)

The agent only names packages and tools; envimage writes the image. Nothing here writes to GitHub.
"""

from __future__ import annotations

import re
import shlex
import uuid
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select

from nightshift import events, outcomes
from nightshift.config import get_settings
from nightshift.db import Repo, RepoContext, session_scope
from nightshift.health.agents import UNTRUSTED_RULE
from nightshift.llm import BudgetExceeded, LLMGateway
from nightshift.memory import get_or_create_repo
from nightshift.router import RouteHint
from nightshift.sandbox.docker import DEP_FILES, Sandbox, SandboxError, prepare_repo
from nightshift.sandbox.envimage import TOOLS, ensure_env_image
from nightshift.worker.verify import CommandResult, run_commands

log = structlog.get_logger()

ENVIRONMENT = "environment"  # RepoContext kind
RECIPE = "recipe"  # RepoContext kind: setup and test commands that worked in an earlier task run

# Files that say how a repo is built and tested, most reliable first, with how much of each to read.
HINTS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^\.github/workflows/[^/]+\.ya?ml$"), 6000),
    (re.compile(r"^\.devcontainer/"), 4000),
    (re.compile(r"^(CONTRIBUTING|DEVELOPMENT|HACKING)[^/]*$|^docs/(contributing|development)[^/]*$", re.I), 8000),
    (re.compile(r"^README[^/]*$", re.I), 10000),
    (re.compile(r"^(Makefile|justfile|noxfile\.py|tox\.ini|\.tool-versions|\.nvmrc|\.node-version"
                r"|\.python-version|rust-toolchain(\.toml)?|go\.mod|pnpm-workspace\.yaml)$"), 3000),
    (re.compile(r"^([^/]+/)?(package\.json|pyproject\.toml|Cargo\.toml|setup\.cfg)$"), 4000),
    (re.compile(r"^([^/]+/)?(Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|compose\.ya?ml)$"), 3000),
]  # fmt: skip
FILES_PER_HINT = 8
MAX_HINT_CHARS = 80_000

# Test output that means the environment is missing something, not that the repo's tests fail.
ENV_ERROR = re.compile(
    r"command not found|No module named|ModuleNotFoundError|Cannot find module|ERR_MODULE_NOT_FOUND"
    r"|error while loading shared libraries|cannot open shared object file"
)

Blocker = Literal["none", "services", "secrets", "gpu", "os", "network", "other"]
Tool = Literal["pnpm", "yarn", "go", "rust"]
assert set(Tool.__args__) == set(TOOLS)  # type: ignore[attr-defined]


class Proposal(BaseModel):
    status: Literal["try", "done", "blocked"] = Field(
        description="try: run this environment next. done: accept the last attempt. blocked: tests cannot run here"
    )
    reasoning: str = Field(description="One or two sentences: what the evidence says and why this step")
    system_packages: list[str] = Field(description="Debian 12 (bookworm) apt package names, no versions")
    tools: list[Tool] = Field(description="Extra toolchains, only from the allowed list")
    setup_commands: list[str] = Field(description="Run in order from the repo root; safe to run again")
    test_commands: list[str] = Field(description="The repo's main unit test suites, as CI runs them")
    blocker: Blocker = Field(description="Why the tests cannot run here, when status is blocked; else none")
    blocker_detail: str = Field(description="For a blocker: what exactly is needed, e.g. 'PostgreSQL 15 on :5432'")
    sources: list[str] = Field(description="The repo files this is based on, e.g. .github/workflows/ci.yml")


class Attempt(BaseModel):
    """One environment tried in the sandbox, and how far it got."""

    system_packages: list[str]
    tools: list[str]
    setup_commands: list[str]
    test_commands: list[str]
    stage: Literal["image", "setup", "tests"]  # the last stage reached
    error: str = ""  # why the image or the setup failed
    tests: list[CommandResult] = []
    image: str = ""
    note: str = ""  # NightShift's reply when the agent's verdict was refused

    @property
    def passed(self) -> bool:
        return self.stage == "tests" and bool(self.tests) and all(t.ok for t in self.tests)

    def env_errors(self) -> list[str]:
        return [t.command for t in self.tests if not t.ok and (t.exit_code == 127 or ENV_ERROR.search(t.tail))]

    def summary(self) -> str:
        if self.stage != "tests":
            return f"{self.stage} failed: {self.error.strip().splitlines()[-1][:200] if self.error.strip() else ''}"
        return f"tests ran: {sum(t.ok for t in self.tests)} of {len(self.tests)} commands passed"

    def text(self, n: int) -> str:
        lines = [f"<attempt n={n}>", f"system_packages: {self.system_packages}", f"tools: {self.tools}"]
        lines += ["setup_commands:", *[f"  {c}" for c in self.setup_commands]]
        lines += ["test_commands:", *[f"  {c}" for c in self.test_commands]]
        if self.stage == "image":
            lines.append(f"The image build failed:\n{self.error}")
        elif self.stage == "setup":
            lines.append(f"The image built. The setup failed:\n{self.error}")
        else:
            lines.append("The image built and the setup succeeded. Test results:")
            lines += [f"{t.line()}\n{t.tail[-2000:]}" for t in self.tests]
        if self.note:
            lines.append(f"NightShift: {self.note}")
        return "\n".join(lines) + "\n</attempt>"


class Environment(BaseModel):
    """How to set a repo up and test it, proven in the sandbox (status ready), or why not."""

    status: Literal["ready", "blocked", "failed"]
    commit: str
    image: str = ""
    system_packages: list[str] = []
    tools: list[str] = []
    setup_commands: list[str] = []
    test_commands: list[str] = []
    tests: list[CommandResult] = []
    blocker: str = ""
    detail: str = ""
    sources: list[str] = []
    attempts: list[str] = []  # one line per attempt
    cost_usd: float = 0.0

    def text(self) -> str:
        """For the Planner."""
        if self.status != "ready":
            return f"status: {self.status}\n{self.blocker}: {self.detail}".strip()
        lines = ["status: ready (these commands were run in the sandbox)", f"system_packages: {self.system_packages}"]
        lines += [f"tools: {self.tools}", "setup_commands:", *[f"  {c}" for c in self.setup_commands]]
        lines += ["test_commands and their result on the default branch:", *[f"  {t.line()}" for t in self.tests]]
        return "\n".join(lines)


SETUP_SYSTEM = f"""You are the Setup agent for NightShift. You work out how to install a GitHub repository's dependencies and run its tests inside a Linux sandbox. The sandbox tries what you propose and you see the result, for a few attempts.

The sandbox: a Debian 12 container with Python 3.12, Node 22 with npm, Bun, uv, git and a C/C++ compiler. Commands run from the repo root as a non-root user with no sudo, each in a fresh bash shell, with network access for package downloads, no services (no database, Redis or Docker) and no secrets. You can add:
- system_packages: Debian 12 (bookworm) apt package names, installed into the image before the setup, e.g. libpq-dev, pkg-config, libssl-dev, openjdk-17-jdk-headless, maven.
- tools, only from this list: pnpm, yarn, go (1.23), rust (1.83).
For another Python version, use uv in the setup, e.g. `uv venv --python 3.11`.

Where to look, most reliable first: CI workflows (they install and test on every push: copy their steps, translated for this container, dropping setup actions, services and sudo), dev container config, the contributing guide, the README, Dockerfiles (only to learn which system packages are needed; they are never built), toolchain files.

- setup_commands install everything the tests need and must be safe to run again on top of an earlier install. Give one plain command per step, no `||` fallback chains: if a command fails you will see why and can change it. Prefer lock files (`uv sync --frozen`, `npm ci`, `pnpm install --frozen-lockfile`). Use CPU-only builds of GPU libraries, e.g. `uv pip install torch --torch-backend cpu`. A virtualenv is not active in the next command: call `.venv/bin/...` directly.
- test_commands run the repo's main unit tests, the ones CI runs, without services. Leave out tests that need services, secrets, a GPU or outside network APIs. Use quiet flags. Each command must finish within 5 minutes here: for a large suite, pick the fast unit tests of the main package (one directory, or a -k selection), not everything. A command that times out is a failed attempt.
- status try: the environment to run next. status done: accept the last attempt, when the setup succeeded and the tests ran, and the failures left are the repo's own failing tests, not a missing tool, package or module. status blocked: the tests cannot run in this sandbox at all (they need a service, secrets, a GPU, another OS, or downloads keep failing); say which in blocker and what exactly in blocker_detail.
- After a failed attempt, read the error and change what caused it. Never repeat an attempt that failed the same way.

{UNTRUSTED_RULE} The repository's files are inside it too."""


async def gather_hints(box: Sandbox) -> tuple[str, list[str]]:
    """The repo files that say how it is built and tested, as one text, and their paths."""
    tracked = (await box.exec("git ls-files")).stdout.splitlines()
    picked: list[tuple[str, int]] = []
    for pattern, chars in HINTS:
        picked += [(p, chars) for p in [p for p in tracked if pattern.search(p)][:FILES_PER_HINT]]
    parts, total, used = [], 0, []
    for path, chars in picked:
        res = await box.exec(f"head -c {chars} -- {shlex.quote(path)}", timeout=30)
        if not res.ok or not res.stdout.strip() or total > MAX_HINT_CHARS:
            continue
        parts.append(f'<file path="{path}">\n{res.stdout}\n</file>')
        total += len(res.stdout)
        used.append(path)
    deps = sorted(p for p in tracked if p.rsplit("/", 1)[-1] in DEP_FILES)[:60]
    parts.append("<dependency_files>\n" + "\n".join(deps) + "\n</dependency_files>")
    return "\n\n".join(parts), used


async def _ask(
    llm: LLMGateway, full_name: str, hints: str, attempts: list[Attempt], previous: Environment | None
) -> Proposal:
    user = f"Repository: {full_name}\n\n<untrusted_github_data>\n{hints}\n</untrusted_github_data>"
    if previous:
        user += f"\n\n<environment_that_worked_before>\n{previous.text()}\n</environment_that_worked_before>"
    if attempts:
        user += "\n\n<attempts>\n" + "\n".join(a.text(i) for i, a in enumerate(attempts, 1)) + "\n</attempts>"
    user += "\n\nPropose the next step." if attempts else "\n\nPropose the first environment to try."
    return await llm.parse(
        agent="setup",
        cfg=get_settings().setup,
        system=SETUP_SYSTEM,
        user=user,
        schema=Proposal,
        cache_key_parts=(),
        label=f"environment, attempt {len(attempts) + 1}",
        # After two failed attempts, a stronger model reads the errors.
        route=RouteHint(level=1 if sum(not a.passed for a in attempts) >= 2 else 0),
    )


async def try_environment(full_name: str, commit: str, p: Proposal) -> Attempt:
    """Build the image, run the setup and the tests in a fresh sandbox. A successful setup saves the repo image."""
    attempt = Attempt(
        system_packages=p.system_packages, tools=list(p.tools), setup_commands=p.setup_commands,
        test_commands=p.test_commands, stage="image",
    )  # fmt: skip
    try:
        attempt.image = await ensure_env_image(p.system_packages, list(p.tools))
    except SandboxError as e:
        attempt.error = str(e)[-3000:]
        return attempt
    attempt.stage = "setup"
    try:
        box, _ = await prepare_repo(full_name, commit, p.setup_commands, image=attempt.image)
    except SandboxError as e:
        attempt.error = str(e)[-3000:]
        return attempt
    async with box:
        attempt.stage = "tests"
        attempt.tests = await run_commands(box, p.test_commands, timeout=get_settings().setup_test_seconds)
    return attempt


def _environment(status: Literal["ready", "failed"], commit: str, a: Attempt, p: Proposal | None) -> Environment:
    return Environment(
        status=status, commit=commit, image=a.image, system_packages=a.system_packages, tools=a.tools,
        setup_commands=a.setup_commands, test_commands=a.test_commands, tests=a.tests,
        sources=p.sources if p else [],
        detail="" if status == "ready" else a.summary(),
    )  # fmt: skip


async def find_environment(
    llm: LLMGateway,
    full_name: str,
    commit: str,
    hints: str,
    previous: Environment | None = None,
    recipe: dict[str, Any] | None = None,
) -> Environment:
    """Ask, try, read the result, and ask again, until an environment runs the tests or the limits are hit.

    A `recipe` (commands that worked in an earlier task here) is tried first, with no model call: when it
    still works, that is the environment; when it does not, the agent starts from its failure."""
    settings = get_settings()
    attempts: list[Attempt] = []
    spent, asks, p, result = 0.0, 0, None, None
    if recipe and recipe.get("passing"):
        known = Proposal(
            status="try", reasoning="commands that worked in an earlier task here", system_packages=[], tools=[],
            setup_commands=recipe.get("setup", []), test_commands=recipe["passing"], blocker="none",
            blocker_detail="", sources=["an earlier NightShift task in this repo"],
        )  # fmt: skip
        await events.record(
            llm.run_id, "node_start", "environment: trying the commands that worked before", node="setup"
        )
        tried = await try_environment(full_name, commit, known)
        if tried.passed:
            result = _environment("ready", commit, tried, known)
        else:
            tried.note = "These commands worked in an earlier task in this repo; they do not work now."
            attempts.append(tried)
    while result is None:
        if spent >= settings.setup_budget_usd or asks >= 2 * settings.setup_attempts:
            break
        try:
            p = await _ask(llm, full_name, hints, attempts, previous)
        except BudgetExceeded:
            break
        spent += llm.last_cost
        asks += 1
        last = attempts[-1] if attempts else None
        if p.status == "blocked":
            result = Environment(
                status="blocked", commit=commit, blocker=p.blocker, detail=p.blocker_detail or p.reasoning,
                sources=p.sources,
            )  # fmt: skip
        elif p.status == "done" and last and last.stage == "tests" and not last.env_errors():
            result = _environment("ready", commit, last, p)
            result.detail = f"accepted with failing tests: {p.reasoning}"
        elif len(attempts) >= settings.setup_attempts:
            result = _environment("failed", commit, last, p) if last else None
        elif p.status == "done" and last:  # refused: the agent sees why on its next turn
            last.note = (
                "Not accepted as done: these test commands look like the environment is missing something "
                f"(command or module not found): {', '.join(last.env_errors()) or 'the setup or image failed'}."
            )
        elif not p.test_commands:
            attempts.append(
                Attempt(**_request(p), stage="setup", error="No test_commands: give the commands that run the tests.")
            )
        else:
            await events.record(llm.run_id, "node_start", f"environment attempt {len(attempts) + 1}", node="setup")
            attempt = await try_environment(full_name, commit, p)
            attempts.append(attempt)
            log.info("setup_attempt", repo=full_name, n=len(attempts), result=attempt.summary())
            if attempt.passed:
                result = _environment("ready", commit, attempt, p)
            else:
                await events.record(llm.run_id, "warning", f"environment attempt {len(attempts)}: {attempt.summary()}")
    if result is None:  # out of budget or turns
        why = f"stopped after {len(attempts)} attempts (${spent:.2f})"
        result = (
            _environment("failed", commit, attempts[-1], p) if attempts else Environment(status="failed", commit=commit)
        )
        result.detail = f"{why}; last: {result.detail}" if attempts else why
    result.attempts = [a.summary() for a in attempts]
    result.cost_usd = round(spent, 4)
    if result.status != "blocked":  # a blocked repo says nothing about the model
        why = "environment ready" if result.status == "ready" else result.detail
        model = getattr(llm, "last_model", "")
        await outcomes.record("setup", model, ok=result.status == "ready", kind="task", run_id=llm.run_id, detail=why)
    return result


def _request(p: Proposal) -> dict[str, list[str]]:
    return {
        "system_packages": p.system_packages, "tools": list(p.tools),
        "setup_commands": p.setup_commands, "test_commands": p.test_commands,
    }  # fmt: skip


# ---------- saved per repo ----------


async def load_environment(repo_id: int) -> Environment | None:
    async with session_scope() as s:
        row = (
            await s.execute(
                select(RepoContext)
                .where(RepoContext.repo_id == repo_id, RepoContext.kind == ENVIRONMENT)
                .order_by(RepoContext.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return Environment.model_validate(row.data) if row else None


async def save_environment(repo_id: int, env: Environment) -> None:
    async with session_scope() as s:
        row = (
            await s.execute(
                select(RepoContext).where(
                    RepoContext.repo_id == repo_id, RepoContext.kind == ENVIRONMENT, RepoContext.commit == env.commit
                )
            )
        ).scalar_one_or_none()
        if row:  # a redo at the same commit replaces the old result, and counts as the newest
            await s.delete(row)
            await s.flush()
        s.add(RepoContext(repo_id=repo_id, kind=ENVIRONMENT, commit=env.commit, data=env.model_dump(mode="json"),
                          cost_usd=env.cost_usd))  # fmt: skip


async def load_recipe(repo_id: int) -> dict[str, Any] | None:
    """The setup and test commands that last worked in a task run here: {"setup", "tests", "passing"}."""
    async with session_scope() as s:
        row = (
            await s.execute(
                select(RepoContext)
                .where(RepoContext.repo_id == repo_id, RepoContext.kind == RECIPE)
                .order_by(RepoContext.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return row.data if row else None


async def ensure_environment(
    llm: LLMGateway, repo: Repo, box: Sandbox, commit: str, *, force: bool = False
) -> Environment:
    """The repo's environment. A ready one is reused; one that failed is retried once the repo moves on.
    `box` has the repo checked out at `commit`, for reading its files."""
    previous = await load_environment(repo.id)
    if previous and not force and (previous.status == "ready" or previous.commit == commit):
        return previous
    await events.record(llm.run_id, "node_start", "environment", node="setup")
    hints, _ = await gather_hints(box)
    env = await find_environment(
        llm, repo.full_name, commit, hints, previous if previous and previous.status == "ready" else None,
        recipe=await load_recipe(repo.id),
    )  # fmt: skip
    await save_environment(repo.id, env)
    return env


def setup_run_id(repo_id: int) -> str:
    return f"setup-{repo_id}-{uuid.uuid4().hex[:8]}"


async def setup_repo(full_name: str, run_id: str | None = None) -> Environment:
    """Find the repo's environment again, at the head of its default branch, as a job of its own."""
    async with session_scope() as s:
        repo = await get_or_create_repo(s, full_name)
    run_id = run_id or setup_run_id(repo.id)
    await events.start_run(run_id, repo.id)
    llm = LLMGateway(run_id, repo.id, run_budget_usd=get_settings().setup_budget_usd)
    try:
        box, _ = await prepare_repo(full_name, "HEAD", [])
        async with box:
            commit = (await box.exec("git rev-parse HEAD")).stdout.strip()
            env = await ensure_environment(llm, repo, box, commit, force=True)
    except BaseException as e:
        await events.finish_run(run_id, "failed", error=f"{type(e).__name__}: {e}")
        raise
    await events.finish_run(run_id, "done" if env.status == "ready" else "stopped", error=env.detail or None)
    return env
