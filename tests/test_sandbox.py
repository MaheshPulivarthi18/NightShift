"""Sandbox guarantees, checked against a real container. Skipped when Docker or the base image is missing."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from nightshift.config import get_settings
from nightshift.sandbox.docker import Sandbox, _docker


def _image_ready() -> bool:
    if not shutil.which("docker"):
        return False
    probe = ["docker", "image", "inspect", get_settings().sandbox_image]
    return subprocess.run(probe, capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _image_ready(), reason="needs Docker and `nightshift sandbox build`")


async def test_container_is_unprivileged_and_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-should-never-leak")
    async with await Sandbox.start() as box:
        who = await box.exec("id -u", workdir="/work")
        assert who.stdout.strip() == "1000"
        env = await box.exec("env", workdir="/work")
        assert "sk-should-never-leak" not in env.stdout
        assert "TOKEN" not in env.stdout and "API_KEY" not in env.stdout
        caps = await box.exec("grep CapEff /proc/self/status", workdir="/work")
        assert caps.stdout.split()[-1] == "0000000000000000"
        write = await box.exec("touch /opt/nightshift/x", workdir="/work")
        assert not write.ok  # NightShift's scripts are mounted read-only


async def test_timeout_kills_the_command_and_pipefail_reports_failures() -> None:
    async with await Sandbox.start() as box:
        slow = await box.exec("sleep 30", workdir="/work", timeout=2)
        assert slow.timed_out and slow.seconds < 20
        piped = await box.exec("false | cat", workdir="/work")
        assert not piped.ok


async def test_container_is_removed_on_exit() -> None:
    async with await Sandbox.start() as box:
        name = box.name
    gone = await _docker("inspect", name)
    assert not gone.ok
