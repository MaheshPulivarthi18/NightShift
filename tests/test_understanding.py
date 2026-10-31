"""The understanding check before publishing: questions about the change, graded answers, a warning at Gate 2
that never blocks. The model is faked; uses the local Postgres (docker compose up), skipped when it is not running."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete

from nightshift.db import Repo, Task, session_scope
from nightshift.llm import LLMGateway
from nightshift.worker import understanding
from nightshift.worker.understanding import Grade, Grades, Question, Questions, gate_warning, passed, pr_rules

BRIEF = """Sarama is a Go client for Kafka.

PR rules:
- Add a `Signed-off-by` line.
- Be able to clearly explain and defend the changes without relying on AI tools.

Setup and test (from the docs):
- go test -race"""


def _grades(*verdicts: str) -> list[Grade]:
    return [Grade(verdict=v, feedback="f") for v in verdicts]  # type: ignore[arg-type]


def test_passing_needs_no_miss_and_all_but_one_understood() -> None:
    assert passed(_grades("understood", "understood", "partly"))
    assert not passed(_grades("understood", "partly", "partly"))
    assert not passed(_grades("understood", "understood", "missed"))


def test_the_repos_ai_rules_come_from_its_brief() -> None:
    rules = pr_rules(BRIEF)
    assert rules[0] == "Add a `Signed-off-by` line." and rules[1].endswith("without relying on AI tools.")
    assert len(rules) == 2
    assert pr_rules("(no repo brief)") == []


def test_gate_2_warns_until_the_check_is_passed() -> None:
    warning = gate_warning({"brief": BRIEF})
    assert warning and warning.startswith("You have not taken the understanding check")
    assert "without relying on AI tools" in warning  # the repo's own rule, quoted
    failed = {"understanding": {"attempts": [{"passed": False}]}}
    assert (gate_warning(failed) or "").startswith("You have not passed")
    assert gate_warning({"understanding": {"attempts": [{"passed": False}, {"passed": True}]}}) is None


@pytest.fixture
async def ready_task() -> AsyncIterator[int]:
    name = f"test-understand/{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name)
            s.add(repo)
            await s.flush()
            issue = {"number": 7, "title": "add() subtracts", "body": "", "state": "open", "author": "o",
                     "labels": [], "assignees": [], "comments": [], "linked_prs": [],
                     "url": "https://github.com/o/r/issues/7"}  # fmt: skip
            diff = "-    return a - b\n+    return a + b"
            t = Task(repo_id=repo.id, issue_number=7, status="ready", base_commit="abc", issue=issue,
                     plan={"markdown": "# Plan"}, result={"brief": BRIEF}, diff=diff)  # fmt: skip
            s.add(t)
            await s.flush()
            task_id = t.id
    except OSError as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    yield task_id
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_questions_then_graded_answers_are_kept_on_the_task(
    ready_task: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def parse(self: LLMGateway, *, schema: type[Any], user: Any, **kw: Any) -> Any:
        seen.append(str(user))
        if schema is Questions:
            return Questions(questions=[Question(question=f"Q{i}", about="calc.py:2", expects=[f"point {i}"])
                                        for i in range(1, 4)])  # fmt: skip
        return Grades(grades=_grades("understood", "understood", "partly"))

    monkeypatch.setattr(LLMGateway, "parse", parse)
    first = await understanding.ask(ready_task)
    assert [q["question"] for q in first["questions"]] == ["Q1", "Q2", "Q3"]
    assert all("expects" not in q for q in first["questions"])  # the points stay hidden until the first answers
    assert "return a + b" in seen[0]  # the questions are about this diff
    graded = await understanding.answer(ready_task, ["it subtracted", "now it adds", "fails on 2+3"])
    assert graded["passed"] and graded["attempts"] == 1
    assert graded["questions"][0]["expects"] == ["point 1"]
    assert "<answer n=3>\nfails on 2+3" in seen[1]
    async with session_scope() as s:
        stored = (await s.get(Task, ready_task)).result["understanding"]  # type: ignore[union-attr]
    assert stored["attempts"][0]["answers"][0] == "it subtracted"
    assert gate_warning({"understanding": stored}) is None
    with pytest.raises(understanding.TaskError, match="Answer all 3"):
        await understanding.answer(ready_task, ["only one"])


def _brief(*rules: str) -> dict[str, str]:
    return {
        "brief": "A repo.\n\nPR rules:\n"
        + "\n".join(f"- {r}" for r in rules)
        + "\n\nSetup and test (from the docs):\n- x"
    }


def test_a_repo_that_asks_for_an_assisted_by_trailer_gets_one() -> None:
    """OpenTelemetry Python: disclose substantial AI-generated commit content with an `Assisted-by:` trailer."""
    otel = _brief(
        "Do not post AI-generated comments on issues or pull requests.",
        "Disclose substantial AI-generated commit content with an `Assisted-by:` commit message trailer.",
    )
    line, warnings = understanding.attribution(otel, {}, "")
    assert line == "Assisted-by: NightShift"
    assert warnings[0].startswith("This repo asks AI-assisted commits to say so with an `Assisted-by:` trailer")
    saved_line, _ = understanding.attribution(otel, {"attribution": ""}, "")  # the developer's saved choice wins
    assert saved_line == ""


def test_a_repo_that_asks_for_disclosure_in_the_pr_is_named() -> None:
    django = _brief("Select exactly one AI assistance disclosure option; if AI tools were used, name them.")
    line, warnings = understanding.attribution(django, {}, "")
    assert line == "" and warnings == [
        "This repo asks you to disclose AI help in the pull request: Select exactly one AI assistance disclosure "
        "option; if AI tools were used, name them."
    ]
    sarama = _brief("Be able to clearly explain and defend the changes without relying on AI tools.")
    assert understanding.attribution(sarama, {}, "Generated by NightShift") == ("Generated by NightShift", [])
