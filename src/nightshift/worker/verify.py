"""Checks code runs on every fix; the model's claims never count as evidence.

1. Baseline: the repo's test commands are run before any change, so a failure that was already there
   is not blamed on the fix.
2. Fail before, pass after: the source changes are taken out and the targeted test must FAIL; they are
   put back and it must PASS. A test that passes without the fix proves nothing.
3. No regressions: every test command that passed at baseline must still pass.

4. The issue's reproduction (when the plan has one, and it failed before any change) must pass with the
   fix: the targeted test can pass while the issue's own case still fails.

A docs change (the plan's kind is "docs") has no failing-first test: `check_docs` instead proves that
only documentation files changed and that the suites still pass. Whether what the docs say is true is
the Reviewer's job, against the code.
"""

from __future__ import annotations

import re
import shlex

from pydantic import BaseModel

from nightshift.sandbox.docker import ExecResult, Sandbox

_TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|testing)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$|(^|/)conftest\.py$"
)
# Documentation: prose files and anything under a docs folder. A docstring is code, so it is not here.
_DOC_RE = re.compile(r"(^|/)docs?/|\.(md|mdx|rst|txt|adoc)$|(^|/)(README|CHANGELOG|CONTRIBUTING|AUTHORS)[^/]*$")
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


class FixCheck(BaseModel):
    passed: bool
    kind: str = "code"  # or "docs": no failing-first test; only documentation files may change
    test_files: list[str]
    source_files: list[str]
    fails_without_fix: CommandResult | None = None
    passes_with_fix: CommandResult | None = None
    suite: list[CommandResult] = []
    reproduction: CommandResult | None = None  # the issue's reproduction, run with the fix
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
        if self.fails_without_fix:
            out.append(f"Without the fix: {self.fails_without_fix.line()}\n{self.fails_without_fix.tail[-1500:]}")
        if self.passes_with_fix:
            out.append(f"With the fix: {self.passes_with_fix.line()}\n{self.passes_with_fix.tail[-1500:]}")
        if self.reproduction:
            r = self.reproduction
            out.append(f"Issue reproduction with the fix: {r.line()}" + ("" if r.ok else f"\n{r.tail[-1500:]}"))
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
    suite: list[str],
    baseline: list[CommandResult],
    reproduction: dict[str, str] | None = None,
) -> FixCheck:
    """`reproduction`: the plan's, when it failed on the code before any change; it must pass now."""
    files = await changed_files(box, base)
    tests = [p for p in files if is_test_path(p)]
    sources = [p for p in files if not is_test_path(p)]
    check = FixCheck(passed=False, test_files=tests, source_files=sources)
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

    await _suites(check, box, suite, baseline)
    check.passed = not check.problems
    return check


async def check_docs(box: Sandbox, base: str, suite: list[str], baseline: list[CommandResult]) -> FixCheck:
    """A docs change: documentation files changed, nothing else did, and the suites still pass."""
    files = await changed_files(box, base)
    docs = [p for p in files if is_doc_path(p)]
    check = FixCheck(passed=False, kind="docs", test_files=[], source_files=docs)
    if not docs:
        check.problems.append("no documentation file was changed")
    if others := [p for p in files if not is_doc_path(p)]:
        check.problems.append(
            "a docs change must not touch other files; undo these or ask for a code task: " + ", ".join(others)
        )
    if check.problems:
        return check
    await _suites(check, box, suite, baseline)
    check.passed = not check.problems
    return check


async def _suites(check: FixCheck, box: Sandbox, suite: list[str], baseline: list[CommandResult]) -> None:
    """Run the suites and compare with the baseline: a command that passed before must pass now."""
    check.suite = await run_commands(box, suite)
    was_ok = {r.command: r.ok for r in baseline}
    for r in check.suite:
        if not r.ok and was_ok.get(r.command, True):
            check.regressions.append(r.command)
        elif not r.ok:
            check.already_failing.append(r.command)
    if check.regressions:
        check.problems.append("these passed before the change and fail now: " + ", ".join(check.regressions))
