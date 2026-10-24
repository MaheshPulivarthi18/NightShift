"""A stand-in for OpenRouter's chat API and model catalog, for gateway, chat and agent tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from nightshift import catalog
from nightshift.catalog import ModelInfo

ALL = ["tools", "structured_outputs", "response_format", "reasoning"]
MODELS = [
    ModelInfo(id="anthropic/claude-opus-5.5", prompt=4e-6, completion=2e-5, supported=ALL,
              efforts=["low", "medium", "high", "xhigh", "max"], max_output=128000),
    ModelInfo(id="anthropic/claude-sonnet-5.5", prompt=2e-6, completion=1e-5, supported=ALL,
              efforts=["low", "medium", "high", "xhigh", "max"]),
    ModelInfo(id="deepseek/deepseek-v4.1-flash", prompt=2e-8, completion=1e-6, supported=ALL),
    ModelInfo(id="openai/gpt-5.6-sol", prompt=4e-6, completion=2e-5, supported=ALL),
    ModelInfo(id="openai/gpt-5.6-luna", prompt=2e-7, completion=1.2e-6, supported=ALL),
    ModelInfo(id="old/no-tools", prompt=1e-6, completion=1e-6, supported=["response_format"]),
]  # fmt: skip


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> list[ModelInfo]:
    """A fixed catalog, so tests never fetch OpenRouter's list or read the stored one."""
    monkeypatch.setattr(catalog, "_models", {m.id: m for m in MODELS})
    monkeypatch.setattr(catalog, "_loaded_at", datetime.now(UTC))
    return MODELS


def reply(
    content: str | None = None,
    *,
    tool_calls: list[tuple[str, str, Any]] | None = None,
    finish: str = "stop",
    cost: float = 0.001,
    refusal: str | None = None,
) -> dict[str, Any]:
    """A chat completion. tool_calls: (id, name, arguments as a dict or a raw string)."""
    calls = [
        {"id": i, "type": "function", "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a)}}
        for i, n, a in tool_calls or []
    ]
    message: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": calls or None, "refusal": refusal}
    return {
        "id": "gen-test",
        "provider": "TestCloud",
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "cost": cost,
                  "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}},
    }  # fmt: skip


class FakeOpenRouter:
    """Answers each request with the next reply (the last one repeats); records every request body."""

    def __init__(self, *replies: dict[str, Any], chunks: list[list[dict[str, Any]]] | None = None) -> None:
        self.replies, self.chunks = list(replies), list(chunks or [])
        self.bodies: list[dict[str, Any]] = []

    async def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    async def stream(self, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        self.bodies.append(body)
        for chunk in self.chunks.pop(0):
            yield chunk
