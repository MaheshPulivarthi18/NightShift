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

import asyncio
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

# Failures with one known cause: NightShift names the fix outright, so an attempt is not spent rediscovering it.
# The header -> package map covers the build errors seen most; others still show the header for the agent.
HEADER_PACKAGES = {
    "libpq-fe.h": "libpq-dev", "ffi.h": "libffi-dev", "openssl/ssl.h": "libssl-dev", "zlib.h": "zlib1g-dev",
    "yaml.h": "libyaml-dev", "sqlite3.h": "libsqlite3-dev", "libxml/xmlversion.h": "libxml2-dev",
    "libxslt/xslt.h": "libxslt1-dev", "krb5.h": "libkrb5-dev", "bzlib.h": "libbz2-dev", "lzma.h": "liblzma-dev",
    "jpeglib.h": "libjpeg-dev", "png.h": "libpng-dev", "freetype/freetype.h": "libfreetype-dev",
    "curl/curl.h": "libcurl4-openssl-dev", "mysql.h": "default-libmysqlclient-dev", "readline/readline.h":
    "libreadline-dev", "ncurses.h": "libncurses-dev", "gmp.h": "libgmp-dev", "snappy-c.h": "libsnappy-dev",
}  # fmt: skip
KNOWN_CAUSES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"No module named pip\b"),
     "uv makes virtualenvs without pip: install with `uv pip install --python .venv/bin/python ...`, not `python -m pip`."),
    (re.compile(r"externally-managed-environment"),
     "The system Python is managed by the OS: install into a virtualenv in the repo (`uv venv .venv`)."),
    (re.compile(r"pg_config executable not found|pg_config: (?:command )?not found"),
     "pg_config comes with libpq-dev: add it to system_packages."),
    (re.compile(r"(?:can't find|could not find) Rust compiler|cargo: (?:command )?not found|rustc: (?:command )?not found", re.I),
     "A package builds Rust code: add `rust` to tools."),
    (re.compile(r"\bgo: (?:command )?not found"), "Add `go` to tools."),
    (re.compile(r"\bpnpm: (?:command )?not found"), "Add `pnpm` to tools."),
    (re.compile(r"\byarn: (?:command )?not found"), "Add `yarn` to tools."),
    (re.compile(r"Permission denied.*(?:/usr/|/opt/|/root|/etc/)|EACCES.*(?:/usr/|/opt/)|are you root\?", re.I),
     "There is no root in the sandbox: install into the repo (a .venv, node_modules, GEM_HOME=$PWD/.gems), never system paths."),
    (re.compile(r"(?:requires|Requires)[- ]Python\s*[<>=!~]+\s*([\d.]+)"),
     "The project needs another Python version: `uv python install <version>` and make the venv with it."),
    (re.compile(r"ENOSPC|No space left on device"), "The sandbox ran out of disk: install fewer extras (no GPU builds, no docs)."),
]  # fmt: skip


def known_causes(output: str) -> list[str]:
    """Exact fixes for failures with one known cause, found in an attempt's output."""
    out = [hint for pattern, hint in KNOWN_CAUSES if pattern.search(output)]
    for header in re.findall(r"fatal error: ([\w/.+-]+\.h): No such file", output):
        package = HEADER_PACKAGES.get(header)
        out.append(f"The C header {header} is missing: add {package} to system_packages."
                   if package else f"The C header {header} is missing: add the -dev package that ships it.")  # fmt: skip
    return list(dict.fromkeys(out))


# A test command runs a test runner. `ls tests/` "passed" as one in multilspy's environment, proving nothing.
TEST_RUNNER = re.compile(
    r"\b(?:pytest|py\.test|unittest|nose2?|tox|nox|hatch\s+test|go\s+test|cargo\s+(?:test|nextest)"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test[\w:-]*|bun\s+test|jest|vitest|mocha|ava|karma|playwright\s+test"
    r"|mvnw?\b.*\b(?:test|verify)\b|gradlew?\b.*\btest|dotnet\s+test|rspec|rake\s+(?:test|spec)|phpunit"
    r"|ctest|make\s+(?:test|check)|meson\s+test|swift\s+test|mix\s+test|stack\s+test|cabal\s+test|deno\s+test"
    r"|runtests|zig\s+build\s+test)\b",
    re.I,
)
# A file that holds tests, for counting how much of the suite the test commands cover
TEST_FILE = re.compile(
    r"(?:^|/)test_[^/]+\.py$|_tests?\.py$|_test\.(?:go|rs|exs)$|\.(?:test|spec)\.[cm]?[jt]sx?$|_spec\.rb$"
    r"|(?:^|/)[^/]*Tests?\.(?:java|kt|cs|swift)$|(?:^|/)tests?/[^/]+\.rs$"
)


def not_test_commands(commands: list[str]) -> list[str]:
    """The commands that run no test runner (listing files, echoing, checking a version)."""
    return [c for c in commands if not TEST_RUNNER.search(c)]


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
    coverage: dict[str, Any] = {}  # how many of the repo's test files the test commands cover (test_coverage)

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
        if hints := known_causes(self.error + "\n".join(t.tail for t in self.tests)):
            lines += ["Known causes NightShift recognises in this output:", *[f"- {h}" for h in hints]]
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
    coverage: dict[str, Any] = {}  # {"files", "total", "left_out"}: how much of the suite the test commands run
    cost_usd: float = 0.0

    def text(self) -> str:
        """For the Planner."""
        if self.status != "ready":
            return f"status: {self.status}\n{self.blocker}: {self.detail}".strip()
        lines = ["status: ready (these commands were run in the sandbox)", f"system_packages: {self.system_packages}"]
        lines += [f"tools: {self.tools}", "setup_commands:", *[f"  {c}" for c in self.setup_commands]]
        lines += ["test_commands and their result on the default branch:", *[f"  {t.line()}" for t in self.tests]]
        if note := coverage_note(self.coverage):
            lines.append(note)
        return "\n".join(lines)


SETUP_SYSTEM = f"""You are the Setup agent for NightShift. You work out how to install a GitHub repository's dependencies and run its tests inside a Linux sandbox. The sandbox tries what you propose and you see the result, for a few attempts.

The sandbox: a Debian 12 container with Python 3.12, Node 22 with npm, Bun, uv, git and a C/C++ compiler. Commands run from the repo root as a non-root user with no sudo, each in a fresh bash shell, with network access for package downloads, no services (no database, Redis or Docker) and no secrets. You can add:
- system_packages: Debian 12 (bookworm) apt package names, installed into the image before the setup, e.g. libpq-dev, pkg-config, libssl-dev, openjdk-17-jdk-headless, maven. Ask only for what the setup or the tests need, and the smallest variant (-headless, -dev, never -doc or full desktop stacks): an image build that runs past its time limit is stopped.
- tools, only from this list: pnpm, yarn, go (1.23), rust (1.83).
For another Python version, use uv in the setup, e.g. `uv venv --python 3.11`.

Where to look, most reliable first: CI workflows (they install and test on every push: copy their steps, translated for this container, dropping setup actions, services and sudo), dev container config, the contributing guide, the README, Dockerfiles (only to learn which system packages are needed; they are never built), toolchain files.

- setup_commands install everything the tests need and must be safe to run again on top of an earlier install. Give one plain command per step, no `||` fallback chains: if a command fails you will see why and can change it. Prefer lock files (`uv sync --frozen`, `npm ci`, `pnpm install --frozen-lockfile`). Use CPU-only builds of GPU libraries, e.g. `uv pip install torch --torch-backend cpu`. A virtualenv is not active in the next command: call `.venv/bin/...` directly.
- test_commands run the repo's main unit tests, the ones CI runs, without services. Leave out tests that need services, secrets, a GPU or outside network APIs. Use quiet flags. Each command must finish within 5 minutes here: for a large suite, pick the fast unit tests of the main package (one directory, or a -k selection), not everything. A command that times out is a failed attempt. Every test command runs a test runner (pytest, go test, npm test …): one that only lists or prints files is refused. NightShift counts how much of the suite they cover and tells the Planner what is left out.
- status try: the environment to run next. status done: accept the last attempt, when the setup succeeded and the tests ran, and the failures left are the repo's own failing tests, not a missing tool, package or module. status blocked: the tests cannot run in this sandbox at all (they need a service, secrets, a GPU, another OS, or downloads keep failing); say which in blocker and what exactly in blocker_detail.
- There is no root: install every language package inside the repo, never system-wide. Python into a .venv; Node without -g; Ruby with the gem directory on the same line, e.g. `GEM_HOME=$PWD/.gems gem install solargraph` (and `GEM_HOME=$PWD/.gems PATH=$PWD/.gems/bin:$PATH` before the commands that use it), since each command runs in a fresh shell.
- A native extension that fails to compile almost always lacks a -dev system package. Find the compiler's real error above the "Results logged to" or "failed to build" line and add the package: yaml.h → libyaml-dev, ffi.h → libffi-dev, openssl → libssl-dev, zlib.h → zlib1g-dev, libxml → libxml2-dev, Python.h → python3-dev.
- Spend attempts on the repo's own language and main suite. Tests that only exercise an optional integration (another language's toolchain or language server that the repo wraps, a cloud SDK, a GUI) are not the main suite: leave them out of test_commands rather than installing their toolchain, unless every test needs it.
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
        attempt.tests = await run_commands(
            box, p.test_commands, timeout=get_settings().setup_test_seconds, note="test, proving the environment"
        )
        attempt.coverage = await test_coverage(box, p.test_commands)
    return attempt


def coverage_note(cov: dict[str, Any]) -> str:
    """One line on how much of the suite the test commands run, and what they leave out."""
    if not cov or not cov.get("total"):
        return ""
    if cov["files"] >= cov["total"]:
        return f"The test commands cover the whole suite ({cov['total']} test files)."
    left = "; ".join(cov.get("left_out", []))
    return (
        f"The test commands cover {cov['files']} of the repo's {cov['total']} test files (approximately)"
        + (f". Not run: {left}" if left else "")
        + ". A change outside what they cover is not checked by them."
    )


async def test_coverage(box: Sandbox, commands: list[str]) -> dict[str, Any]:
    """How many of the repo's test files the commands cover: a command naming no paths runs the whole suite
    (`pytest`, `go test ./...`); one naming files or folders covers the test files under them. Approximate:
    a `-k` selection or a filter inside a folder still counts the whole folder."""
    tracked = (await box.exec("git ls-files")).stdout.splitlines()
    tests = [f for f in tracked if TEST_FILE.search(f)]
    if not tests:
        return {}
    dirs = {d for f in tracked for d in _parents(f)}
    covered: set[str] = set()
    for command in commands:
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        named = [w.rstrip("/").removeprefix("./") for w in words if not w.startswith("-")]
        named = [w for w in named if w in dirs or w in tracked or w.split("::", 1)[0] in tracked]
        if not named or "./..." in words:
            return {"files": len(tests), "total": len(tests), "left_out": []}
        for w in named:
            path = w.split("::", 1)[0]
            covered |= {t for t in tests if t == path or t.startswith(path + "/")}
    groups: dict[str, int] = {}
    for t in tests:
        if t not in covered:
            group = "/".join(t.split("/")[:2]) if t.count("/") >= 2 else t.rsplit("/", 1)[0] if "/" in t else "."
            groups[group] = groups.get(group, 0) + 1
    left = [f"{g}/ ({n} files)" for g, n in sorted(groups.items(), key=lambda x: -x[1])[:6]]
    return {"files": len(covered), "total": len(tests), "left_out": left}


def _parents(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _environment(status: Literal["ready", "failed"], commit: str, a: Attempt, p: Proposal | None) -> Environment:
    return Environment(
        status=status, commit=commit, image=a.image, system_packages=a.system_packages, tools=a.tools,
        setup_commands=a.setup_commands, test_commands=a.test_commands, tests=a.tests, coverage=a.coverage,
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
    runs_tests = [c for c in (recipe or {}).get("passing", []) if TEST_RUNNER.search(c)]
    if recipe and runs_tests:
        known = Proposal(
            status="try", reasoning="commands that worked in an earlier task here", system_packages=[], tools=[],
            setup_commands=recipe.get("setup", []), test_commands=runs_tests, blocker="none",
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
        elif bad := not_test_commands(p.test_commands):  # refused before it runs: an attempt it cannot pass
            attempts.append(Attempt(**_request(p), stage="setup", error=(
                f"Not run: these test_commands run no test runner: {'; '.join(bad)}. Each test command must run "
                "tests (pytest, go test, npm test, jest, cargo test …), not list, print or inspect files."
            )))  # fmt: skip
        else:
            await events.record(llm.run_id, "node_start", f"environment attempt {len(attempts) + 1}", node="setup")
            attempt = await try_environment(full_name, commit, p)
            attempts.append(attempt)
            log.info("setup_attempt", repo=full_name, n=len(attempts), result=attempt.summary())
            if attempt.passed:
                result = _environment("ready", commit, attempt, p)
            else:
                # The Setup agent reads the failure and tries again, until its budget or turns run out
                await events.record(
                    llm.run_id, events.CAUGHT, f"environment attempt {len(attempts)}: {attempt.summary()}"
                )
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


async def environment_for(llm: LLMGateway, repo: Repo, commit: str) -> Environment:
    """The repo's environment at `commit`, opening a sandbox to read the repo's files when it must be found."""
    previous = await load_environment(repo.id)
    if previous and previous.status == "ready":
        return previous
    async with await Sandbox.start() as box:
        await box.exec(
            f"git clone --quiet --filter=blob:none https://github.com/{repo.full_name}.git repo", workdir="/work"
        )
        await box.exec(f"git checkout --quiet --force --detach {shlex.quote(commit)}")
        return await ensure_environment(llm, repo, box, commit)


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
    except asyncio.CancelledError:  # the server stopped (closed, restarted): say so, not "CancelledError"
        await asyncio.shield(events.finish_run(run_id, "interrupted", error=events.INTERRUPTED))
        raise
    except BaseException as e:
        await events.finish_run(run_id, "failed", error=f"{type(e).__name__}: {e}")
        raise
    await events.finish_run(run_id, "done" if env.status == "ready" else "stopped", error=env.detail or None)
    return env
