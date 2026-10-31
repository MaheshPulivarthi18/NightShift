"""A model request that never finishes is given up after a deadline, so the gateway can try the next model.
OpenRouter keeps a slow request alive, so the read timeout alone never fires (a review once hung 15 minutes)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from nightshift.config import get_settings
from nightshift.llm import ModelAPIError, OpenRouter


@pytest.fixture
def short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "model_call_seconds", 0.3)


async def test_a_request_that_never_answers_is_given_up(short_deadline: None) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"choices": []})

    client = OpenRouter(http=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    started = time.monotonic()
    with pytest.raises(ModelAPIError, match="no answer within") as e:
        await client.complete({"model": "x", "messages": []})
    assert e.value.status == 504 and time.monotonic() - started < 2  # not retried five times on the same model
    await client.http.aclose()


async def test_a_request_whose_cancelling_hangs_is_still_given_up(short_deadline: None) -> None:
    """Cancelling a request on a dead connection can itself hang; the deadline does not wait for it."""

    async def stuck(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(3)  # cleaning up takes long, like a TLS close on a dead connection
            raise
        return httpx.Response(200, json={"choices": []})

    client = OpenRouter(http=httpx.AsyncClient(transport=httpx.MockTransport(stuck)))
    started = time.monotonic()
    with pytest.raises(ModelAPIError, match="no answer within"):
        await client.complete({"model": "x", "messages": []})
    assert time.monotonic() - started < 2


async def test_a_silent_stream_is_given_up(short_deadline: None) -> None:
    async def silent() -> AsyncIterator[bytes]:
        yield b": OPENROUTER PROCESSING\n\n"
        await asyncio.sleep(5)  # then nothing at all: the per-line check never ran
        yield b"data: [DONE]\n\n"

    client = OpenRouter(
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=silent())))
    )
    started = time.monotonic()
    with pytest.raises(ModelAPIError, match="no complete answer within"):
        async for _ in client.stream({"model": "x", "messages": []}):
            pass
    assert time.monotonic() - started < 2


async def test_a_stream_kept_alive_but_never_finishing_is_given_up(short_deadline: None) -> None:
    async def keepalives() -> AsyncIterator[bytes]:
        for _ in range(50):
            yield b": OPENROUTER PROCESSING\n\n"
            await asyncio.sleep(0.05)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=keepalives())

    client = OpenRouter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ModelAPIError, match="no complete answer within"):
        async for _ in client.stream({"model": "x", "messages": []}):
            pass
    await client.http.aclose()
