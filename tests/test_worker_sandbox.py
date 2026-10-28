"""The worker's code-enforced checks, against a tiny repo in a real container. Skipped without Docker."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from test_sandbox import pytestmark as needs_docker

from nightshift.sandbox.docker import REPO_DIR, Sandbox
from nightshift.worker.coder import CoderRun, ToolError, safe_path
from nightshift.worker.verify import (
    Scope,
    check_docs,
    check_fix,
    check_scope,
    check_suites,
    is_doc_path,
    is_test_path,
    run_commands,
)

pytestmark = needs_docker

CALC = "def add(a, b):\n    return a - b\n"
OTHER = "def double(x):\n    return x * 2\n"
TEST = "from calc import add\nassert add(2, 3) == 5\nprint('ok')\n"
TARGETED = "python tests/test_calc.py"
SUITE = ["python -c 'from other import double; assert double(2) == 4'"]


@pytest.fixture
async def repo() -> AsyncIterator[tuple[Sandbox, str]]:
    async with await Sandbox.start() as box:
        await box.exec(f"mkdir -p {REPO_DIR}", workdir="/work")
        await box.write_file(f"{REPO_DIR}/calc.py", CALC)
        await box.write_file(f"{REPO_DIR}/other.py", OTHER)
        await box.exec(
            "git init -q && git -c user.email=t@t -c user.name=t add -A && "
            "git -c user.email=t@t -c user.name=t commit -qm base"
        )
        base = (await box.exec("git rev-parse HEAD")).stdout.strip()
        yield box, base


async def _add_test(box: Sandbox, body: str = TEST) -> None:
    await box.exec("mkdir -p tests")
    await box.write_file(f"{REPO_DIR}/tests/test_calc.py", "import sys; sys.path.insert(0, '.')\n" + body)


async def test_a_real_fix_passes(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    await _add_test(box)
    await box.write_file(f"{REPO_DIR}/calc.py", CALC.replace("a - b", "a + b"))
    check = await check_fix(box, base, TARGETED)
    assert check.passed, check.report()
    assert check.fails_without_fix and not check.fails_without_fix.ok
    assert (await box.exec("cat calc.py")).stdout == CALC.replace("a - b", "a + b")  # fix put back


async def test_a_test_that_passes_without_the_fix_is_rejected(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    await _add_test(box, "print('nothing checked')\n")
    await box.write_file(f"{REPO_DIR}/calc.py", CALC.replace("a - b", "a + b"))
    check = await check_fix(box, base, TARGETED)
    assert not check.passed
    assert any("PASSES without the fix" in p for p in check.problems)


async def test_a_regression_is_caught_and_new_source_files_survive(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    baseline = await run_commands(box, SUITE)
    await _add_test(box)
    await box.write_file(f"{REPO_DIR}/calc.py", "from helper import plus as add\n")
    await box.write_file(f"{REPO_DIR}/helper.py", "def plus(a, b):\n    return a + b\n")
    await box.write_file(f"{REPO_DIR}/other.py", OTHER.replace("x * 2", "x * 3"))
    check = await check_fix(box, base, TARGETED)
    assert check.passed and not check.suites_run  # submit proves the fix; the suites run after the review
    await check_suites(check, box, SUITE, baseline)
    assert not check.passed
    assert check.regressions == SUITE
    assert (await box.exec("test -f helper.py")).ok  # the new file was restored after the fail-before run


async def test_coder_tools_stay_inside_the_repo(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    coder = CoderRun(box, None, base, SUITE, [], "")  # type: ignore[arg-type]
    for bad in ("/etc/passwd", "../x", ".git/config", ""):
        with pytest.raises(ToolError):
            safe_path(bad)
    text, is_error = await coder.tool("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})
    assert not is_error, text
    text, is_error = await coder.tool("edit_file", {"path": "calc.py", "old_string": "missing", "new_string": "x"})
    assert is_error and "0 times" in text
    text, is_error = await coder.tool("read_file", {"path": "calc.py"})
    assert "return a + b" in text


def test_test_paths() -> None:
    for p in ("tests/test_x.py", "engine/tests/conftest.py", "ui/src/a.test.ts", "pkg/x_test.go", "src/__tests__/a.js"):
        assert is_test_path(p), p
    for p in ("engine/laya/api/chat_api.py", "ui/src/lib/utils/toolLabels.ts", "contest.py"):
        assert not is_test_path(p), p


async def test_a_docs_change_passes_with_docs_only(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    await box.write_file(f"{REPO_DIR}/README.md", "# calc\n\n`add(a, b)` returns the sum.\n")
    check = await check_docs(box, base)
    assert check.passed, check.report()
    assert check.kind == "docs" and check.source_files == ["README.md"]
    assert "Docs change" in check.report()


async def test_a_docs_change_may_not_touch_code(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    await box.write_file(f"{REPO_DIR}/README.md", "# calc\n")
    await box.write_file(f"{REPO_DIR}/calc.py", CALC.replace("a - b", "a + b"))
    check = await check_docs(box, base)
    assert not check.passed
    assert any("must not touch other files" in p and "calc.py" in p for p in check.problems)
    nothing = await check_docs(box, base)  # still no docs-only change
    assert not nothing.passed


def test_doc_paths() -> None:
    for p in ("README.md", "docs/guide/install.rst", "doc/index.txt", "CONTRIBUTING", "pkg/README", "CHANGELOG.md"):
        assert is_doc_path(p), p
    for p in ("src/multilspy/clangd.py", "docsify.py", "tests/test_readme.py", "readme_utils.ts"):
        assert not is_doc_path(p), p


async def test_the_repos_own_tools_come_first_on_path(repo: tuple[Sandbox, str]) -> None:
    box, _ = repo
    await box.exec("mkdir -p .venv/bin node_modules/.bin")
    await box.write_file(f"{REPO_DIR}/.venv/bin/pytest", "#!/bin/sh\necho repo-pytest\n")
    await box.write_file(f"{REPO_DIR}/node_modules/.bin/jest", "#!/bin/sh\necho repo-jest\n")
    await box.exec("chmod +x .venv/bin/pytest node_modules/.bin/jest")
    assert (await box.exec("pytest && jest")).stdout.split() == ["repo-pytest", "repo-jest"]


# The issue's own case: add() must work for floats too, which the targeted test does not check.
REPRO = {
    "script_name": "repro_7.py",
    "script": "import sys; sys.path.insert(0, '.')\nfrom calc import add\nassert add(0.5, 0.25) == 0.75\n",
    "command": "python /tmp/nightshift-repro/repro_7.py",
}


async def test_the_issues_reproduction_must_pass_with_the_fix(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    await _add_test(box)
    await box.write_file(f"{REPO_DIR}/calc.py", CALC.replace("a - b", "a + b"))
    check = await check_fix(box, base, TARGETED, reproduction=REPRO)
    assert check.passed, check.report()
    assert check.reproduction is not None and check.reproduction.ok
    assert "Issue reproduction with the fix" in check.report()
    assert (await box.exec("git status --porcelain")).stdout.strip().count("repro") == 0  # never part of the diff


async def test_a_fix_that_passes_its_test_but_not_the_issue_is_caught(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    await _add_test(box)
    # Makes the integer test pass (2 + 3 == 5) but the issue's float case still fails.
    await box.write_file(f"{REPO_DIR}/calc.py", "def add(a, b):\n    return int(a) + int(b)\n")
    check = await check_fix(box, base, TARGETED, reproduction=REPRO)
    assert not check.passed
    assert any("reproduction still fails" in p for p in check.problems)


async def test_an_out_of_scope_change_goes_back_before_any_test_runs(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    await _add_test(box)
    await box.write_file(f"{REPO_DIR}/calc.py", CALC.replace("a - b", "a + b"))
    await box.write_file(f"{REPO_DIR}/uv.lock", "# re-locked by an install\n")
    check = await check_fix(box, base, TARGETED, Scope(planned=["calc.py"], size="small"))
    assert not check.passed and check.fails_without_fix is None  # caught for free: no test ran
    assert check.scope is not None and check.scope.generated == ["uv.lock"]
    await box.exec("rm uv.lock")
    check = await check_fix(box, base, TARGETED, Scope(planned=["calc.py"], size="small"))
    assert check.passed, check.report()


def test_scope_rules() -> None:
    planned = Scope(planned=["src/a.py", "engine/requirements.lock"], size="small")
    ok = check_scope({"src/a.py": 30, "tests/test_a.py": 900, "engine/requirements.lock": 4000}, planned)
    assert not ok.problems and ok.lines == 30  # tests and a lock file the plan names do not count
    relocked = check_scope({"src/a.py": 30, "ui/package-lock.json": 700, "go.sum": 3}, planned)
    assert relocked.generated == ["ui/package-lock.json", "go.sum"] and relocked.problems
    big = check_scope({"src/a.py": 300, "src/b.py": 200}, planned)
    assert big.lines == 500 and any("past the 400" in p for p in big.problems)
    assert big.unplanned == ["src/b.py"]  # allowed, listed for the Reviewer
