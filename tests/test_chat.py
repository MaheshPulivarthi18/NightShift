"""Chat history handling, and one full turn with a fake model that calls a tool."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fake_openrouter import FakeOpenRouter
from sqlalchemy import delete, select

from nightshift import chat as chat_mod
from nightshift.db import Chat, ChatMessage, Item, LLMCall, Repo, session_scope


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


def _chunk(delta: dict[str, Any], finish: str | None = None, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "gen-1", "provider": "TestCloud", "choices": [{"delta": delta, "finish_reason": finish}],
            **({"usage": usage} if usage else {})}  # fmt: skip


USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.002}
# First call asks for a file (its arguments arrive in two pieces); the second answers.
CHUNKS = [
    [
        _chunk({"tool_calls": [{"index": 0, "id": "tu1", "function": {"name": "read_file", "arguments": '{"pa'}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "src/a.py"}'}}]}, "tool_calls", USAGE),
    ],
    [_chunk({"content": "It is in "}), _chunk({"content": "src/a.py:1."}, "stop", USAGE)],
]


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
        chats = (await s.execute(select(Chat.id).where(Chat.repo_id == repo.id))).scalars().all()
        await s.execute(delete(LLMCall).where(LLMCall.run_id.in_([f"chat:{c}" for c in chats])))  # the chat's spend
        await s.execute(delete(Repo).where(Repo.full_name == name))


async def test_chat_turn_runs_a_tool_and_saves_history(
    repo_and_item: tuple[Repo, Item], monkeypatch: pytest.MonkeyPatch, models: Any
) -> None:
    repo, item = repo_and_item
    ran: list[tuple[str, dict[str, Any]]] = []

    async def fake_run(self: chat_mod.RepoTools, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        ran.append((name, args))
        return "1: def a(): pass", False

    monkeypatch.setattr(chat_mod.RepoTools, "run", fake_run)
    fake = FakeOpenRouter(chunks=[list(c) for c in CHUNKS])
    events = [ev async for ev in chat_mod.chat_turn(repo, item, "Where is it?", client=fake)]  # type: ignore[arg-type]

    assert ran == [("read_file", {"path": "src/a.py"})]
    assert [e["type"] for e in events] == ["tool", "text", "text", "done"]
    assert events[-1]["cost_usd"] > 0
    # The second call carries the tool result, and the cached context names the item.
    second = fake.bodies[1]["messages"]
    assert second[-1] == {"role": "tool", "tool_call_id": "tu1", "content": "1: def a(): pass"}
    assert "It breaks" in fake.bodies[0]["messages"][0]["content"][1]["text"]

    chat = await chat_mod.get_or_create_chat(repo, 7)
    saved = await chat_mod.load_messages(chat.id)
    assert [m.role for m in saved] == ["user", "assistant", "user", "assistant"]
