"""The worker's code-enforced checks, against a tiny repo in a real container. Skipped without Docker."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from test_sandbox import pytestmark as needs_docker

from nightshift.sandbox.docker import REPO_DIR, Sandbox
from nightshift.worker.coder import CoderRun, ToolError, safe_path
from nightshift.worker.verify import check_fix, is_test_path, run_commands

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
    baseline = await run_commands(box, SUITE)
    await _add_test(box)
    await box.write_file(f"{REPO_DIR}/calc.py", CALC.replace("a - b", "a + b"))
    check = await check_fix(box, base, TARGETED, SUITE, baseline)
    assert check.passed, check.report()
    assert check.fails_without_fix and not check.fails_without_fix.ok
    assert (await box.exec("cat calc.py")).stdout == CALC.replace("a - b", "a + b")  # fix put back


async def test_a_test_that_passes_without_the_fix_is_rejected(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    baseline = await run_commands(box, SUITE)
    await _add_test(box, "print('nothing checked')\n")
    await box.write_file(f"{REPO_DIR}/calc.py", CALC.replace("a - b", "a + b"))
    check = await check_fix(box, base, TARGETED, SUITE, baseline)
    assert not check.passed
    assert any("PASSES without the fix" in p for p in check.problems)


async def test_a_regression_is_caught_and_new_source_files_survive(repo: tuple[Sandbox, str]) -> None:
    box, base = repo
    baseline = await run_commands(box, SUITE)
    await _add_test(box)
    await box.write_file(f"{REPO_DIR}/calc.py", "from helper import plus as add\n")
    await box.write_file(f"{REPO_DIR}/helper.py", "def plus(a, b):\n    return a + b\n")
    await box.write_file(f"{REPO_DIR}/other.py", OTHER.replace("x * 2", "x * 3"))
    check = await check_fix(box, base, TARGETED, SUITE, baseline)
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
