"""The tests related to a PR's change, run with the environment's own runner beside its narrow suite."""

from __future__ import annotations

from typing import Any

from nightshift.worker.related_tests import _module, go_packages, related, related_command, related_python

TRACKED = [
    "pydantic/main.py", "pydantic/types.py", "pydantic/_internal/_model_construction.py",
    "tests/test_main.py", "tests/test_types.py", "tests/test_edge_cases.py", "tests/test_json.py",
    "tests/conftest.py", "pydantic/_internal/tests/test_construct.py", "docs/index.md",
]  # fmt: skip


class Box:
    def __init__(self, grep_hits: list[str]) -> None:
        self.grep_hits, self.commands = grep_hits, []

    async def exec(self, command: str, **kw: Any) -> Any:
        self.commands.append(command)
        out = "\n".join(TRACKED) if command.startswith("git ls-tree") else "\n".join(self.grep_hits)
        return type("R", (), {"stdout": out, "ok": True})()


def test_module_names() -> None:
    assert _module("pydantic/main.py") == "pydantic.main"
    assert _module("src/pkg/sub/__init__.py") == "pkg.sub"


async def test_related_tests_rank_name_then_folder_then_imports() -> None:
    box = Box(grep_hits=["tests/test_edge_cases.py", "tests/test_main.py"])
    found = await related_python(box, ["pydantic/main.py", "pydantic/_internal/_model_construction.py", "docs/x.md"])  # type: ignore[arg-type]
    # test_main.py by name; the _internal tests folder by place; test_edge_cases.py imports pydantic.main
    assert found == ["tests/test_main.py", "pydantic/_internal/tests/test_construct.py", "tests/test_edge_cases.py"]
    assert "-e pydantic.main" in box.commands[1] and "-e pydantic._internal._model_construction" in box.commands[1]
    assert await related_python(Box([]), ["docs/index.md", "tests/test_main.py"]) == []  # type: ignore[arg-type]


def test_the_command_reuses_the_environments_runner() -> None:
    files = ["tests/test_main.py", "tests/test_edge_cases.py"]
    pydantic = ["./.venv/bin/python -m pytest tests/test_main.py tests/test_types.py -q -p no:cacheprovider"]
    assert related_command(pydantic, files, []) == (
        "./.venv/bin/python -m pytest tests/test_main.py tests/test_edge_cases.py -q -p no:cacheprovider"
    )
    otel = ["uv run --no-sync pytest opentelemetry-sdk/tests -q"]
    assert related_command(otel, files, []).startswith("uv run --no-sync pytest tests/test_main.py")  # type: ignore[union-attr]
    nested = ["cd engine && .venv/bin/pytest tests -q"]
    assert (
        related_command(nested, ["engine/tests/test_a.py", "ui/tests/test_b.py"], [])
        == "cd engine && .venv/bin/pytest tests/test_a.py -q -p no:cacheprovider"
    )
    assert related_command(["npm test"], files, []) is None  # not a runner this module can reuse


def test_go_runs_the_changed_packages_from_their_modules() -> None:
    """opentelemetry-go-contrib has a go.mod per instrumentation: `go test` runs from each module's folder."""
    modules = ["go.mod", "instrumentation/net/http/otelhttp/go.mod", "exporters/autoexport/go.mod"]
    changed = ["instrumentation/net/http/otelhttp/handler.go", "instrumentation/net/http/otelhttp/internal/x.go",
               "exporters/autoexport/metrics.go", "tools.go", "README.md"]  # fmt: skip
    go = go_packages(changed, modules)
    assert go == {"": ["."], "exporters/autoexport": ["."],
                  "instrumentation/net/http/otelhttp": [".", "./internal"]}  # fmt: skip
    assert related_command(["go test ./..."], [], go) == (
        "go test . && (cd exporters/autoexport && go test .) && (cd instrumentation/net/http/otelhttp && "
        "go test . ./internal)"
    )
    assert related_command(["make test-short"], [], go) is not None  # the suite runs through make
    assert go_packages(["a/b.go"]) == {"": ["./a"]}  # one module at the root


async def test_the_report_says_what_ran() -> None:
    command, note = await related(Box(["tests/test_edge_cases.py"]), ["pydantic/main.py"], ["uv run pytest tests -q"])  # type: ignore[arg-type]
    assert command == "uv run pytest tests/test_main.py tests/test_edge_cases.py -q -p no:cacheprovider"
    assert note == "Also ran the 2 test files related to the change: tests/test_main.py, tests/test_edge_cases.py."
    assert await related(Box([]), ["README.md"], ["uv run pytest -q"]) == (None, "")  # type: ignore[arg-type]
