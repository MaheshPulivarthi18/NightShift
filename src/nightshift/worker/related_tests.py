"""The tests related to a change, so a PR check runs more than the environment's narrow suite.

A repo's environment runs a fixed set of test commands, often a small part of the suite (pydantic: 3 of its 257
test files), so a PR that breaks something elsewhere passes. For each source file a PR changes, the related test
files are, best first: a name match (`main.py` -> `test_main.py`), tests in the same folder or its tests/ folder,
and tests that import the module (`pydantic.main`). They are run with the environment's own test runner, on the
base and with the PR, like the suites: one that passed before and fails with the PR is a regression.

Python (pytest) and Go (`go test` on the changed packages) are covered; other languages are left to the suites
and the PR's CI, and the report says so.
"""

from __future__ import annotations

import posixpath
import re
import shlex

from nightshift.sandbox.docker import Sandbox
from nightshift.worker.setup_agent import TEST_FILE
from nightshift.worker.verify import is_doc_path, is_test_path

LIMIT = 15  # test files; more is a suite of its own, slower than the check should be
PYTEST = re.compile(
    r"^(.*?\bpytest)\b"
)  # the runner part of a command: `./.venv/bin/python -m pytest`, `uv run pytest`


def _module(path: str) -> str:
    """`src/pydantic/main.py` -> `pydantic.main` (a leading src/ or lib/ is not part of the import)."""
    parts = path.removesuffix(".py").split("/")
    if parts and parts[0] in ("src", "lib"):
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


async def related_python(box: Sandbox, changed: list[str], limit: int = LIMIT) -> list[str]:
    """Test files related to the changed Python sources, best first. The repo is checked out at the base."""
    sources = [f for f in changed if f.endswith(".py") and not is_test_path(f) and not is_doc_path(f)]
    if not sources:
        return []
    tracked = (await box.exec("git ls-tree -r --name-only HEAD")).stdout.splitlines()
    tests = [t for t in tracked if t.endswith(".py") and TEST_FILE.search(t)]
    ranked: list[str] = []

    def add(found: list[str]) -> None:
        ranked.extend(t for t in found if t not in ranked)

    for src in sources:  # a name match first: main.py -> test_main.py, main_test.py
        stem = posixpath.basename(src).removesuffix(".py")
        add([t for t in tests if posixpath.basename(t) in (f"test_{stem}.py", f"{stem}_test.py")])
    for src in sources:  # then tests beside it, or in a tests/ folder beside it
        folder = posixpath.dirname(src)
        add([t for t in tests if posixpath.dirname(t) in (folder, posixpath.join(folder, "tests"))])
    modules = [m for m in (_module(s) for s in sources) if "." in m]  # a top-level name alone matches too much
    if modules and len(ranked) < limit:
        pattern = " ".join(f"-e {shlex.quote(m)}" for m in modules)
        hits = await box.exec(f"git grep -l -F {pattern} -- {' '.join(shlex.quote(t) for t in tests[:2000])}")
        add([t for t in hits.stdout.splitlines() if t in tests])
    return ranked[:limit]


def go_packages(changed: list[str], modules: list[str] | None = None) -> dict[str, list[str]]:
    """The Go packages the PR changes, grouped by the module (go.mod folder) they belong to: `go test` runs from
    a module's folder, and a multi-module repo (opentelemetry-go-contrib) has one per instrumentation.
    {"" (the root module): ["./exporters/otlp"], "instrumentation/x": ["."]}"""
    roots = sorted((posixpath.dirname(m) for m in (modules or ["go.mod"])), key=len, reverse=True)
    out: dict[str, set[str]] = {}
    for f in changed:
        if not f.endswith(".go"):
            continue
        folder = posixpath.dirname(f)
        root = next((r for r in roots if r == "" or folder == r or folder.startswith(r + "/")), "")
        rel = folder[len(root) :].lstrip("/") if root else folder
        out.setdefault(root, set()).add(f"./{rel}" if rel else ".")
    return {root: sorted(pkgs) for root, pkgs in sorted(out.items())}


def related_command(test_commands: list[str], python_tests: list[str], go: dict[str, list[str]]) -> str | None:
    """One command running the related tests with the environment's own runner, or None when there are none or
    the environment has no runner this module knows."""
    if python_tests:
        for command in test_commands:
            if m := PYTEST.match(command.split("&&")[-1].strip()):
                prefix = command.rsplit("&&", 1)[0] + "&& " if "&&" in command else ""
                # `cd engine && pytest …` runs inside engine/: only its tests, with paths relative to it
                folder = (cd.group(1).strip("/") + "/") if (cd := re.match(r"\s*cd\s+(\S+)", prefix)) else ""
                inside = [t.removeprefix(folder) for t in python_tests if t.startswith(folder)]
                if not inside:
                    continue
                files = " ".join(shlex.quote(t) for t in inside)
                return f"{prefix}{m.group(1)} {files} -q -p no:cacheprovider"
    if go and any(re.search(r"\bgo\s+test\b", c) for c in test_commands):
        parts = [
            f"go test {' '.join(pkgs)}" if not root else f"(cd {shlex.quote(root)} && go test {' '.join(pkgs)})"
            for root, pkgs in go.items()
        ]
        return " && ".join(parts)
    return None


async def related(box: Sandbox, changed: list[str], test_commands: list[str]) -> tuple[str | None, str]:
    """(the command, a line for the report). The repo is checked out at the base."""
    python_tests = await related_python(box, changed)
    go: dict[str, list[str]] = {}
    if any(f.endswith(".go") for f in changed):
        tracked = (await box.exec("git ls-tree -r --name-only HEAD")).stdout.splitlines()
        go = go_packages(changed, [t for t in tracked if posixpath.basename(t) == "go.mod"])
    command = related_command(test_commands, python_tests, go)
    if command and python_tests:
        shown = ", ".join(python_tests[:6]) + (" …" if len(python_tests) > 6 else "")
        return command, f"Also ran the {len(python_tests)} test files related to the change: {shown}."
    if command:
        packages = [
            posixpath.join(root, p.removeprefix("./")).rstrip("/.") or "." for root, ps in go.items() for p in ps
        ]
        return command, f"Also ran the tests of the changed Go packages: {', '.join(packages)}."
    code = [f for f in changed if not is_test_path(f) and not is_doc_path(f)]
    if code and (python_tests or go):
        return None, "Related tests were found, but the environment's test runner is not one this check can reuse."
    return None, ""
