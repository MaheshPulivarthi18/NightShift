"""The Setup agent's loop and the environment images it asks for, without Docker or a model."""

from __future__ import annotations

from typing import Any

import pytest

from nightshift.config import get_settings
from nightshift.sandbox.docker import SandboxError
from nightshift.sandbox.envimage import check_request, dockerfile, env_image_name
from nightshift.worker import setup_agent
from nightshift.worker.setup_agent import Attempt, Proposal, find_environment
from nightshift.worker.verify import CommandResult


def _proposal(status: str = "try", **kw: Any) -> Proposal:
    base: dict[str, Any] = dict(
        status=status, reasoning="", system_packages=[], tools=[], setup_commands=["uv sync"],
        test_commands=["pytest -q"], blocker="none", blocker_detail="", sources=[".github/workflows/ci.yml"],
    )  # fmt: skip
    return Proposal(**(base | kw))


def _result(ok: bool, tail: str = "", code: int | None = None) -> CommandResult:
    return CommandResult(
        command="pytest -q", ok=ok, exit_code=0 if ok else (code or 1), timed_out=False, seconds=1, tail=tail
    )


class _Script:
    """A model that answers with the given proposals in order and records what it was shown."""

    run_id = None  # no events recorded

    def __init__(self, *answers: Proposal) -> None:
        self.answers, self.prompts, self.last_cost = list(answers), [], 0.01

    async def parse(self, **kw: Any) -> Proposal:
        self.prompts.append(kw["user"])
        return self.answers.pop(0)


def _sandbox(monkeypatch: pytest.MonkeyPatch, *outcomes: Attempt) -> list[Proposal]:
    tried: list[Proposal] = []
    queue = list(outcomes)

    async def fake_try(full_name: str, commit: str, p: Proposal) -> Attempt:
        tried.append(p)
        return queue.pop(0).model_copy(update=setup_agent._request(p))

    monkeypatch.setattr(setup_agent, "try_environment", fake_try)
    return tried


def _attempt(stage: str, *, error: str = "", tests: list[CommandResult] | None = None) -> Attempt:
    return Attempt(
        system_packages=[], tools=[], setup_commands=[], test_commands=[], stage=stage, error=error,
        tests=tests or [], image="nightshift-sandbox:3",
    )  # fmt: skip


async def test_passing_tests_on_the_first_try_are_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _sandbox(monkeypatch, _attempt("tests", tests=[_result(True)]))
    env = await find_environment(_Script(_proposal()), "o/r", "abc", "hints")  # type: ignore[arg-type]
    assert env.status == "ready"
    assert env.setup_commands == ["uv sync"] and env.sources == [".github/workflows/ci.yml"]
    assert env.attempts == ["tests ran: 1 of 1 commands passed"]


async def test_a_setup_failure_goes_back_to_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    tried = _sandbox(
        monkeypatch,
        _attempt("setup", error="error: command 'gcc' failed: libpq-fe.h: No such file"),
        _attempt("tests", tests=[_result(True)]),
    )
    llm = _Script(_proposal(), _proposal(system_packages=["libpq-dev"]))
    env = await find_environment(llm, "o/r", "abc", "hints")  # type: ignore[arg-type]
    assert env.status == "ready" and env.system_packages == ["libpq-dev"]
    assert "libpq-fe.h" in llm.prompts[1]  # the agent saw the error
    assert len(tried) == 2


async def test_failing_repo_tests_can_be_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _sandbox(monkeypatch, _attempt("tests", tests=[_result(False, "1 failed, 300 passed")]))
    env = await find_environment(_Script(_proposal(), _proposal("done")), "o/r", "abc", "hints")  # type: ignore[arg-type]
    assert env.status == "ready" and not env.tests[0].ok
    assert env.detail.startswith("accepted with failing tests")


async def test_done_is_refused_while_a_module_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _sandbox(
        monkeypatch,
        _attempt("tests", tests=[_result(False, "ModuleNotFoundError: No module named 'yaml'")]),
        _attempt("tests", tests=[_result(True)]),
    )
    llm = _Script(_proposal(), _proposal("done"), _proposal(setup_commands=["uv sync --all-extras"]))
    env = await find_environment(llm, "o/r", "abc", "hints")  # type: ignore[arg-type]
    assert env.status == "ready" and env.setup_commands == ["uv sync --all-extras"]
    assert "Not accepted as done" in llm.prompts[2]


async def test_blocked_says_what_is_needed() -> None:
    llm = _Script(_proposal("blocked", blocker="services", blocker_detail="PostgreSQL 15 on :5432"))
    env = await find_environment(llm, "o/r", "abc", "hints")  # type: ignore[arg-type]
    assert (env.status, env.blocker, env.detail) == ("blocked", "services", "PostgreSQL 15 on :5432")


async def test_attempts_are_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    limit = get_settings().setup_attempts
    tried = _sandbox(monkeypatch, *[_attempt("setup", error="boom") for _ in range(limit)])
    llm = _Script(*[_proposal() for _ in range(limit + 1)])
    env = await find_environment(llm, "o/r", "abc", "hints")  # type: ignore[arg-type]
    assert env.status == "failed" and len(tried) == limit
    assert env.detail == "setup failed: boom"


def test_env_image_rules() -> None:
    check_request(["libpq-dev", "openjdk-17-jdk-headless", "g++"], ["go", "pnpm"])
    for packages, tools in ([["curl; rm -rf /"], []], [["python3=3.11"], []], [[], ["docker"]]):
        with pytest.raises(SandboxError):
            check_request(packages, tools)
    assert env_image_name([], []) == get_settings().sandbox_image
    assert env_image_name(["libpq-dev"], []) == env_image_name(["libpq-dev", "libpq-dev"], [])
    text = dockerfile("nightshift-sandbox:3", ["libpq-dev"], ["rust"])
    assert "curl libpq-dev" in text and "RUSTUP_HOME" in text and text.rstrip().endswith("USER worker")


RECIPE = {"setup": ["uv sync"], "tests": ["pytest -q", "pytest slow -q"], "passing": ["pytest -q"]}


async def test_a_recipe_that_still_works_needs_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    tried = _sandbox(monkeypatch, _attempt("tests", tests=[_result(True)]))
    llm = _Script()  # no answers: any model call would fail
    env = await find_environment(llm, "o/r", "abc", "hints", recipe=RECIPE)  # type: ignore[arg-type]
    assert env.status == "ready" and env.cost_usd == 0 and llm.prompts == []
    assert tried[0].test_commands == ["pytest -q"]  # only the commands that passed before
    assert env.sources == ["an earlier NightShift task in this repo"]


async def test_a_recipe_that_broke_is_the_agents_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _sandbox(
        monkeypatch,
        _attempt("setup", error="uv: lockfile out of date"),
        _attempt("tests", tests=[_result(True)]),
    )
    llm = _Script(_proposal(setup_commands=["uv sync --upgrade"]))
    env = await find_environment(llm, "o/r", "abc", "hints", recipe=RECIPE)  # type: ignore[arg-type]
    assert env.status == "ready" and env.setup_commands == ["uv sync --upgrade"]
    assert "worked in an earlier task" in llm.prompts[0] and "lockfile out of date" in llm.prompts[0]
