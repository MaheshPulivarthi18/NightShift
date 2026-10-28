"""Checks code runs on every fix; the model's claims never count as evidence.

They run cheapest first, so a change that is wrong is caught before the slow checks run:

1. Scope (free, instant): the change stays inside the plan. A lock file or generated file the plan did not
   name, or a change far bigger than the plan's size, goes back to the Coder before any test runs. Source
   files the plan did not name are allowed but listed, for the Reviewer to judge.
2. Fail before, pass after (seconds): the source changes are taken out and the targeted test must FAIL;
   they are put back and it must PASS. A test that passes without the fix proves nothing.
3. The issue's reproduction (when the plan has one, and it failed before any change) must pass with the
   fix: the targeted test can pass while the issue's own case still fails.
4. No regressions (minutes): once the Reviewer approves, the repo's test commands run, and every one that
   passed at the baseline (run before any change) must still pass. A failure that was already there is
   not blamed on the fix. They run once, not on every round, because they are the slowest check.

The Coder's submit runs 1 to 3; the task runs 4 (task.py), and a regression goes back to the Coder.

A docs change (the plan's kind is "docs") has no failing-first test: `check_docs` instead proves that
only documentation files changed, then the suites run as for code. Whether what the docs say is true is
the Reviewer's job, against the code.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from pydantic import BaseModel

from nightshift.sandbox.docker import ExecResult, Sandbox

_TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|testing)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$|(^|/)conftest\.py$"
)
# Documentation: prose files and anything under a docs folder. A docstring is code, so it is not here.
_DOC_RE = re.compile(r"(^|/)docs?/|\.(md|mdx|rst|txt|adoc)$|(^|/)(README|CHANGELOG|CONTRIBUTING|AUTHORS)[^/]*$")
# Lock files, vendored and built output: regenerated, not written. A change to one the plan did not name
# is almost always a side effect (an install that re-locked everything), never part of the fix.
_GENERATED_RE = re.compile(
    r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|uv\.lock|poetry\.lock"
    r"|Pipfile\.lock|Cargo\.lock|go\.sum|composer\.lock|Gemfile\.lock|flake\.lock)$|\.lock$|requirements[^/]*\.lock$"
    r"|(^|/)(vendor|dist|build|third_party)/|\.min\.(js|css)$"
)
# Changed lines (tests and generated files left out) past which a change is out of scope for the plan's size.
SIZE_LIMITS = {"small": 400, "medium": 1000, "large": 2500}
PATCH = "/tmp/ns-source.patch"
REPRO_DIR = "/tmp/nightshift-repro"  # outside the repo, so the script is never part of the change
# Build and test output that is never part of a change, even in a repo whose .gitignore misses it.
EXCLUDES = [
    "__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", "*.egg-info/", ".coverage",
    "node_modules/", ".venv/", ".svelte-kit/", ".next/", "coverage/", "target/",
]  # fmt: skip


# Run before every verification command. Python checks cached bytecode by mtime (seconds) and size, so a
# source file swapped and swapped back within one second with the same size would run stale code.
FRESH = (
    "find . \\( -name node_modules -o -name .venv \\) -prune -o -name __pycache__ -type d -prune "
    "-exec rm -rf {} + 2>/dev/null; export PYTHONDONTWRITEBYTECODE=1; "
)


async def run_fresh(box: Sandbox, command: str, timeout: float | None = None) -> CommandResult:
    return CommandResult.of(command, await box.exec(FRESH + command, timeout=timeout))


def is_test_path(path: str) -> bool:
    return bool(_TEST_RE.search(path.lower()))


def is_doc_path(path: str) -> bool:
    return bool(_DOC_RE.search(path) or _DOC_RE.search(path.lower()))


def is_generated_path(path: str) -> bool:
    return bool(_GENERATED_RE.search(path))


class CommandResult(BaseModel):
    command: str
    ok: bool
    exit_code: int
    timed_out: bool
    seconds: float
    tail: str

    @classmethod
    def of(cls, command: str, res: ExecResult, chars: int = 3000) -> CommandResult:
        return cls(
            command=command,
            ok=res.ok,
            exit_code=res.code,
            timed_out=res.timed_out,
            seconds=round(res.seconds, 1),
            tail=res.tail(chars),
        )

    def line(self) -> str:
        status = "passed" if self.ok else ("timed out" if self.timed_out else f"failed (exit {self.exit_code})")
        return f"`{self.command}` {status} in {self.seconds:.0f}s"


async def run_commands(box: Sandbox, commands: list[str], timeout: float | None = None) -> list[CommandResult]:
    return [await run_fresh(box, c, timeout) for c in commands]


async def mark_new_files(box: Sandbox) -> None:
    """Make new files show up in `git diff` (intent-to-add), skipping build and test output."""
    excludes = "\n".join(EXCLUDES)
    await box.exec(
        f"grep -qxF '# nightshift' .git/info/exclude 2>/dev/null || printf '\\n# nightshift\\n%s\\n' "
        f"{shlex.quote(excludes)} >> .git/info/exclude; git add --all --intent-to-add"
    )


async def changed_files(box: Sandbox, base: str) -> list[str]:
    await mark_new_files(box)
    res = await box.exec(f"git diff --name-only {shlex.quote(base)}")
    return [p for p in res.stdout.splitlines() if p]


async def diff(box: Sandbox, base: str) -> str:
    await mark_new_files(box)
    return (await box.exec(f"git diff {shlex.quote(base)}")).stdout


def step(name: str, ok: bool, round_no: int, seconds: float, detail: str) -> dict[str, Any]:
    """One entry on a task's Checks: plan, scope, test, review or suites, in the order this module gives."""
    return {"check": name, "ok": ok, "round": round_no, "seconds": round(seconds, 1), "detail": detail}


class Scope(BaseModel):
    """What the plan said the change would be: the files it names and its size."""

    planned: list[str] = []
    size: str = "medium"


class ScopeCheck(BaseModel):
    lines: int = 0  # changed lines, tests and generated files left out
    limit: int = 0
    generated: list[str] = []  # lock, vendored or built files the plan did not name
    unplanned: list[str] = []  # source files the plan did not name: allowed, for the Reviewer to judge
    problems: list[str] = []


async def numstat(box: Sandbox, base: str) -> dict[str, int]:
    """Changed lines per file (added + removed); binary files count as 0."""
    await mark_new_files(box)
    out: dict[str, int] = {}
    for line in (await box.exec(f"git diff --numstat {shlex.quote(base)}")).stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            out[parts[2]] = sum(int(n) for n in parts[:2] if n.isdigit())
    return out


def check_scope(lines: dict[str, int], scope: Scope) -> ScopeCheck:
    """Gate 1: free, so it runs before any test. Problems go back to the Coder."""
    planned = set(scope.planned)
    generated = [p for p in lines if is_generated_path(p) and p not in planned]
    counted = sum(n for p, n in lines.items() if not is_test_path(p) and not is_generated_path(p))
    limit = SIZE_LIMITS.get(scope.size, SIZE_LIMITS["medium"])
    check = ScopeCheck(
        lines=counted,
        limit=limit,
        generated=generated,
        unplanned=[p for p in lines if p not in planned and not is_test_path(p) and not is_generated_path(p)],
    )
    if generated:
        check.problems.append(
            "these lock, vendored or built files changed but the plan does not name them; restore them with "
            "`git checkout <base> -- <file>` (an install or a lock run probably rewrote them): " + ", ".join(generated)
        )
    if counted > limit:
        check.problems.append(
            f"the change is {counted} lines, past the {limit} a {scope.size} plan allows: keep only what the issue "
            "needs (no refactors, reformatting or unrelated fixes)"
        )
    return check


class FixCheck(BaseModel):
    passed: bool
    kind: str = "code"  # or "docs": no failing-first test; only documentation files may change
    test_files: list[str]
    source_files: list[str]
    fails_without_fix: CommandResult | None = None
    passes_with_fix: CommandResult | None = None
    suite: list[CommandResult] = []
    reproduction: CommandResult | None = None  # the issue's reproduction, run with the fix
    scope: ScopeCheck | None = None
    suites_run: bool = False  # the suites run once the Reviewer approves (check_suites), not on submit
    regressions: list[str] = []
    already_failing: list[str] = []
    problems: list[str] = []

    def report(self) -> str:
        """Plain text for the Coder (what to fix) and the Reviewer (what was proven)."""
        out = [f"Verification {'PASSED' if self.passed else 'FAILED'}."]
        if self.kind == "docs":
            out.append("Docs change: no failing-first test; only documentation files may change, suites must pass.")
            out.append(f"Documentation files changed: {', '.join(self.source_files) or 'none'}")
        else:
            out.append(f"Test files changed: {', '.join(self.test_files) or 'none'}")
            out.append(f"Source files changed: {', '.join(self.source_files) or 'none'}")
        out += [f"- Problem: {p}" for p in self.problems]
        if self.scope:
            sc = self.scope
            out.append(f"Scope: {sc.lines} changed lines (limit {sc.limit}).")
            if sc.unplanned:
                out.append("Changed but not named in the plan (judge whether they belong): " + ", ".join(sc.unplanned))
        if self.fails_without_fix:
            out.append(f"Without the fix: {self.fails_without_fix.line()}\n{self.fails_without_fix.tail[-1500:]}")
        if self.passes_with_fix:
            out.append(f"With the fix: {self.passes_with_fix.line()}\n{self.passes_with_fix.tail[-1500:]}")
        if self.reproduction:
            r = self.reproduction
            out.append(f"Issue reproduction with the fix: {r.line()}" + ("" if r.ok else f"\n{r.tail[-1500:]}"))
        if not self.suites_run and (self.passes_with_fix or self.kind == "docs") and not self.problems:
            out.append("Full suites: not run yet; they run once the Reviewer approves, and any regression comes back.")
        for r in self.suite:
            out.append(f"Suite: {r.line()}" + ("" if r.ok else f"\n{r.tail[-2000:]}"))
        if self.already_failing:
            out.append("Already failing before any change (not caused by this fix): " + ", ".join(self.already_failing))
        return "\n".join(out)


async def run_reproduction(box: Sandbox, reproduction: dict[str, str]) -> CommandResult:
    """Save the plan's reproduction script outside the repo and run its command from the repo root."""
    name = re.sub(r"[^\w.-]", "_", reproduction.get("script_name") or "")[:60] or "repro.py"
    await box.exec(f"mkdir -p {REPRO_DIR}")
    await box.write_file(f"{REPRO_DIR}/{name}", reproduction.get("script", ""))
    return await run_fresh(box, reproduction.get("command", ""))


async def check_fix(
    box: Sandbox,
    base: str,
    targeted: str,
    scope: Scope | None = None,
    reproduction: dict[str, str] | None = None,
) -> FixCheck:
    """The checks a submit runs: scope, then fail before / pass after, then the reproduction. The suites
    run later (check_suites). `scope`: the plan's, for a change NightShift wrote; None checks no scope (a PR
    someone else wrote). `reproduction`: the plan's, when it failed before any change; it must pass now."""
    files = await changed_files(box, base)
    tests = [p for p in files if is_test_path(p)]
    sources = [p for p in files if not is_test_path(p)]
    check = FixCheck(passed=False, test_files=tests, source_files=sources)
    if scope is not None:
        check.scope = check_scope(await numstat(box, base), scope)
    if check.scope and check.scope.problems:  # free to find, so nothing slower runs on an out-of-scope change
        check.problems += check.scope.problems
        return check
    if not tests:
        check.problems.append("no test file was added or changed: write a test that reproduces the issue")
    if not sources:
        check.problems.append("no source file was changed: the fix itself is missing")
    if not targeted.strip():
        check.problems.append("no targeted test command was given")
    if check.problems:
        return check

    quoted = " ".join(shlex.quote(p) for p in sources)
    # Only the change's own files are compared: running tests may create other untracked files.
    fingerprint = f"git diff --binary {shlex.quote(base)} -- {' '.join(shlex.quote(p) for p in files)} | sha256sum"
    before = (await box.exec(fingerprint)).stdout
    await box.exec(f"git diff --binary {shlex.quote(base)} -- {quoted} > {PATCH}")
    try:
        for p in sources:
            await box.exec(
                f"if git cat-file -e {shlex.quote(base)}:{shlex.quote(p)} 2>/dev/null; "
                f"then git checkout {shlex.quote(base)} -- {shlex.quote(p)}; "
                f"else git rm --cached --quiet --ignore-unmatch -- {shlex.quote(p)}; rm -f -- {shlex.quote(p)}; fi"
            )
        check.fails_without_fix = await run_fresh(box, targeted)
    finally:
        restored = await box.exec(f"git apply --binary {PATCH}")
        await mark_new_files(box)
    after = (await box.exec(fingerprint)).stdout
    if not restored.ok or before != after:
        check.problems.append(f"could not put the fix back after the fail-before run: {restored.tail(500)}")
        return check

    if check.fails_without_fix.ok:
        check.problems.append("the targeted test PASSES without the fix, so it does not reproduce the issue")
    elif check.fails_without_fix.timed_out:
        check.problems.append("the targeted test timed out without the fix; that proves nothing")

    check.passes_with_fix = await run_fresh(box, targeted)
    if not check.passes_with_fix.ok:
        check.problems.append("the targeted test FAILS with the fix")
    if reproduction:
        check.reproduction = await run_reproduction(box, reproduction)
        if not check.reproduction.ok:
            check.problems.append("the issue's reproduction still fails with the fix, so the issue is not fixed")

    check.passed = not check.problems
    return check


async def check_docs(box: Sandbox, base: str) -> FixCheck:
    """A docs change: documentation files changed and nothing else did. The suites run later (check_suites)."""
    files = await changed_files(box, base)
    docs = [p for p in files if is_doc_path(p)]
    check = FixCheck(passed=False, kind="docs", test_files=[], source_files=docs)
    if not docs:
        check.problems.append("no documentation file was changed")
    if others := [p for p in files if not is_doc_path(p)]:
        check.problems.append(
            "a docs change must not touch other files; undo these or ask for a code task: " + ", ".join(others)
        )
    check.passed = not check.problems
    return check


async def check_suites(check: FixCheck, box: Sandbox, suite: list[str], baseline: list[CommandResult]) -> None:
    """The last and slowest check, once the Reviewer approves: run the suites and compare with the baseline.
    A command that passed before must pass now; a regression fails the check."""
    check.suites_run, check.regressions, check.already_failing = True, [], []
    check.suite = await run_commands(box, suite)
    was_ok = {r.command: r.ok for r in baseline}
    for r in check.suite:
        if not r.ok and was_ok.get(r.command, True):
            check.regressions.append(r.command)
        elif not r.ok:
            check.already_failing.append(r.command)
    if check.regressions:
        check.problems.append("these passed before the change and fail now: " + ", ".join(check.regressions))
    check.passed = not check.problems
