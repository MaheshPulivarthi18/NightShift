"""A setup that needs a lock file, run on a commit older than the lock file."""

from __future__ import annotations

import pytest

from nightshift.sandbox.docker import NO_LOCKFILE, unlocked


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("uv sync --locked --extra all", "uv sync --extra all"),
        ("uv sync --frozen", "uv sync"),
        ("pnpm install --frozen-lockfile", "pnpm install"),
        ("yarn install --immutable", "yarn install"),
        ("npm ci --prefix frontend", "npm install --prefix frontend"),
        ("pip install -e .", None),
        ("go mod download", None),
    ],
)
def test_unlocked(command: str, expected: str | None) -> None:
    assert unlocked(command) == expected


def test_missing_lockfile_messages() -> None:
    uv = "error: Unable to find lockfile at `uv.lock`, but `--locked` was provided."
    npm = "npm ERR! The `npm ci` command can only install with an existing package-lock.json"
    assert NO_LOCKFILE.search(uv) and NO_LOCKFILE.search(npm)
    assert not NO_LOCKFILE.search("error: The lockfile at `uv.lock` needs to be updated, but `--locked` was provided.")
