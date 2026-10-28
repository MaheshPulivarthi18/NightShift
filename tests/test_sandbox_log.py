"""What happens in the sandbox shows on the run's log: a shown command's start, its output while it runs, and
its result; housekeeping commands stay out, and anything token-shaped is masked."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from test_sandbox import pytestmark as needs_docker

from nightshift import events
from nightshift.db import RunEvent, session_scope
from nightshift.sandbox import docker
from nightshift.sandbox.docker import Sandbox

pytestmark = needs_docker


@pytest.fixture
async def run_id() -> AsyncIterator[str]:
    rid = f"test-log-{uuid.uuid4().hex[:8]}"
    try:
        async with session_scope() as s:
            await s.execute(select(1))
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available: {e}")
    token = events._current.set(rid)
    yield rid
    events._current.reset(token)
    async with session_scope() as s:
        await s.execute(delete(RunEvent).where(RunEvent.run_id == rid))


async def _logged(run_id: str) -> list[RunEvent]:
    async with session_scope() as s:
        rows = await s.execute(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id))
        return list(rows.scalars())


async def test_a_shown_command_streams_its_output_while_it_runs(run_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker, "FLUSH_SECONDS", 0.2)
    key = "sk-or-v1-" + "a" * 32
    async with await Sandbox.start() as box:
        await box.exec("git --version", workdir="/tmp")  # housekeeping: not on the log
        res = await box.exec(
            f"echo first; sleep 1; echo {key}; echo done", show="install things", note="setup", workdir="/tmp"
        )
    assert res.ok
    logged = await _logged(run_id)
    kinds = [e.kind for e in logged]
    assert kinds[0] == events.CMD_START and kinds[-1] == events.CMD_END
    assert kinds.count(events.CMD_OUTPUT) >= 2  # "first" arrived before the command finished
    assert len({e.data["cmd"] for e in logged}) == 1
    start, end = logged[0], logged[-1]
    assert start.message == "install things" and start.data["note"] == "setup"
    assert end.data["exit"] == 0 and not end.data["timed_out"] and "done" in end.data["tail"]
    output = "".join(e.message for e in logged if e.kind == events.CMD_OUTPUT)
    assert "first" in output and key not in output + end.data["tail"] and "[redacted]" in output


async def test_a_failing_command_shows_its_exit_code(run_id: str) -> None:
    async with await Sandbox.start() as box:
        res = await box.exec("echo broken >&2; exit 3", show="pytest -q", workdir="/tmp")
    assert res.code == 3
    end = (await _logged(run_id))[-1]
    assert end.kind == events.CMD_END and end.data["exit"] == 3 and "broken" in end.data["tail"]


async def test_nothing_is_logged_without_a_run() -> None:
    assert events.current_run() is None
    async with await Sandbox.start() as box:
        assert (await box.exec("echo hi", show="echo hi", workdir="/tmp")).ok  # no run: a plain call, nowhere to log it


def test_token_shaped_text_is_masked() -> None:
    text = "ok ghp_" + "b" * 36 + " and Bearer " + "c" * 24 + " AKIA" + "D" * 16 + " sk-not-a-key"
    masked = events.redact(text)
    assert masked.count("[redacted]") == 3 and masked.startswith("ok ") and masked.endswith("sk-not-a-key")
