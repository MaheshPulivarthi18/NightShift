"""The Setup agent wastes fewer attempts: known causes are named outright, log files a failure points at are
attached, and a setup that failed part way resumes from its last slow step that succeeded."""

from __future__ import annotations

import subprocess
import time

import pytest
from test_sandbox import pytestmark as needs_docker

from nightshift.sandbox import docker
from nightshift.sandbox.docker import LOG_PATH, SandboxError, checkpoint_name, prepare_repo, repo_image_name
from nightshift.worker.setup_agent import Attempt, known_causes

REPO = "octocat/Hello-World"  # tiny and public


def test_known_causes_are_named_with_their_fix() -> None:
    out = "\n".join([
        "/work/repo/.venv/bin/python: No module named pip",
        "psycopg2/psycopg.h:36:10: fatal error: libpq-fe.h: No such file or directory",
        "src/x.c:2:10: fatal error: weird/thing.h: No such file or directory",
        "OSError: [Errno 13] Permission denied: '/usr/local/lib/python3.12/site-packages/x'",
    ])  # fmt: skip
    hints = known_causes(out)
    assert hints[0].startswith("uv makes virtualenvs without pip")
    assert "The C header libpq-fe.h is missing: add libpq-dev to system_packages." in hints
    assert "The C header weird/thing.h is missing: add the -dev package that ships it." in hints
    assert any(h.startswith("There is no root in the sandbox") for h in hints)
    assert known_causes("all good") == []


def test_an_attempt_shows_the_hints_to_the_agent() -> None:
    a = Attempt(system_packages=[], tools=[], setup_commands=["pip install -e ."], test_commands=[], stage="setup",
                error="setup command `pip install -e .` failed (exit 1):\nNo module named pip")  # fmt: skip
    assert "Known causes NightShift recognises in this output:\n- uv makes virtualenvs without pip" in a.text(1)


def test_log_files_named_in_a_failure_are_found() -> None:
    out = (
        "error: see /tmp/pip-build-xyz/build.log for details\n"
        "meson failed, log at /work/repo/build/meson-logs/meson-log.txt."
    )
    assert LOG_PATH.findall(out) == ["/tmp/pip-build-xyz/build.log", "/work/repo/build/meson-logs/meson-log.txt"]
    assert LOG_PATH.findall("no paths, just text.log or ./rel/x.log") == []


def _images(name: str) -> list[str]:
    out = subprocess.run(["docker", "images", name, "--format", "{{.Repository}}:{{.Tag}}"],
                         capture_output=True, text=True).stdout  # fmt: skip
    return [t for t in out.split() if not t.endswith(":<none>")]


@needs_docker
async def test_a_failed_setup_resumes_from_its_last_slow_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker, "CHECKPOINT_SECONDS", 2)
    slow = "sleep 3 && echo installed > /work/repo/.done"
    try:
        try:
            await prepare_repo(REPO, "HEAD", [slow, "echo broken && exit 1"])
        except SandboxError as e:
            if "clone" in str(e):  # pragma: no cover - no network
                pytest.skip(f"GitHub not reachable: {e}")
            assert "setup command `echo broken && exit 1` failed" in str(e)
        assert len(_images(checkpoint_name(REPO))) == 1  # the slow step that succeeded was kept
        started = time.monotonic()
        box, cached = await prepare_repo(REPO, "HEAD", [slow, "test -f /work/repo/.done"])
        took = time.monotonic() - started
        try:
            assert not cached and (await box.exec("cat .done")).stdout.strip() == "installed"
        finally:
            await box.remove()
        assert took < 30 and _images(checkpoint_name(REPO)) == []  # resumed, then dropped once the setup was whole
    finally:
        for tag in _images(checkpoint_name(REPO)) + _images(repo_image_name(REPO)):
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
