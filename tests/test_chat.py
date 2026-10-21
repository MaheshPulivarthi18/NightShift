"""Chat history handling, and one full turn with a fake model that calls a tool."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete

from nightshift import chat as chat_mod
from nightshift.db import ChatMessage, Item, Repo, session_scope


def _msg(role: str, content: list[dict[str, Any]]) -> ChatMessage:
    return ChatMessage(chat_id=1, role=role, content=content)


def test_history_trims_old_tool_output_and_starts_at_user_text() -> None:
    msgs = [
        _msg("user", [{"type": "tool_result", "tool_use_id": "x", "content": "orphan"}]),
        _msg("user", [{"type": "text", "text": "q1"}]),
        _msg("assistant", [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}}]),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 5000}]),
        _msg("assistant", [{"type": "text", "text": "a1"}]),
        _msg("user", [{"type": "text", "text": "q2"}]),
    ]
    out = chat_mod.history_for_api(msgs)
    assert out[0]["content"][0]["text"] == "q1"  # the orphan tool_result is dropped
    assert len(out[2]["content"][0]["content"]) == chat_mod.OLD_TOOL_RESULT_CHARS


def test_dangling_tool_call_gets_an_error_result() -> None:
    msgs = [
        _msg("user", [{"type": "text", "text": "q1"}]),
        _msg("assistant", [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]),
        _msg("user", [{"type": "text", "text": "q2"}]),  # the turn was interrupted before the tool ran
    ]
    out = chat_mod.history_for_api(msgs)
    assert out[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "Interrupted before the tool ran.",
        "is_error": True,
    }
    assert out[2]["content"][1]["text"] == "q2"


class _Block(SimpleNamespace):
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v is not None}


class _FakeStream:
    def __init__(self, final: Any, texts: list[str]) -> None:
        self.final, self.texts = final, texts

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            for t in self.texts:
                yield SimpleNamespace(type="text", text=t)

        return gen()

    async def get_final_message(self) -> Any:
        return self.final


class _FakeMessages:
    """First call asks for a file; second call answers."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        usage = SimpleNamespace(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        if len(self.calls) == 1:
            block = _Block(type="tool_use", id="tu1", name="read_file", input={"path": "src/a.py"})
            final = SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)
            return _FakeStream(final, [])
        final = SimpleNamespace(
            content=[_Block(type="text", text="It is in src/a.py:1.")], stop_reason="end_turn", usage=usage
        )
        return _FakeStream(final, ["It is in ", "src/a.py:1."])


@pytest.fixture
async def repo_and_item() -> AsyncIterator[tuple[Repo, Item]]:
    name = f"test/chat-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    try:
        async with session_scope() as s:
            repo = Repo(full_name=name, role="maintainer")
            s.add(repo)
            await s.flush()
            item = Item(
                repo_id=repo.id,
                number=7,
                kind="issue",
                title="Bug",
                body="It breaks",
                author="u",
                state="OPEN",
                url=f"https://github.com/{name}/issues/7",
                labels=[],
                data={"comments": []},
                content_hash="h",
                created_at=now,
                updated_at=now,
            )
            s.add(item)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    yield repo, item
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_chat_turn_runs_a_tool_and_saves_history(
    repo_and_item: tuple[Repo, Item], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, item = repo_and_item
    ran: list[tuple[str, dict[str, Any]]] = []

    async def fake_run(self: chat_mod.RepoTools, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        ran.append((name, args))
        return "1: def a(): pass", False

    monkeypatch.setattr(chat_mod.RepoTools, "run", fake_run)
    fake = _FakeMessages()
    client = SimpleNamespace(messages=fake)
    events = [ev async for ev in chat_mod.chat_turn(repo, item, "Where is it?", client=client)]  # type: ignore[arg-type]

    assert ran == [("read_file", {"path": "src/a.py"})]
    assert [e["type"] for e in events] == ["tool", "text", "text", "done"]
    assert events[-1]["cost_usd"] > 0
    # The second call carries the tool result, and the cached context names the item.
    second = fake.calls[1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert "It breaks" in fake.calls[0]["system"][1]["text"]

    chat = await chat_mod.get_or_create_chat(repo, 7)
    saved = await chat_mod.load_messages(chat.id)
    assert [m.role for m in saved] == ["user", "assistant", "user", "assistant"]
