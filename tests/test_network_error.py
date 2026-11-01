"""Which sandbox failures count as the network (retried once) rather than the repo."""

from __future__ import annotations

import pytest

from nightshift.sandbox.docker import NETWORK_ERROR


@pytest.mark.parametrize(
    "output",
    [
        'go: github.com/spf13/pflag@v1.0.5: Get "https://proxy.golang.org/...v1.0.5.zip": unexpected EOF',
        "error: Failed to download `torch==2.5.1`\n  Caused by: error decoding response body",
        "Caused by: request or response body error",
        "ProtocolError('Connection broken: IncompleteRead(1024 bytes read, 2048 more expected)')",
        "npm ERR! network socket hang up",
        "curl: (56) Recv failure: Connection reset by peer",
        "Read timed out.",
    ],
)
def test_dropped_downloads_are_network_errors(output: str) -> None:
    assert NETWORK_ERROR.search(output)


@pytest.mark.parametrize(
    "output",
    [
        "error: No solution found when resolving dependencies",
        "FAILED tests/test_m0.py::test_x - AssertionError",
        "go: cannot find main module",
    ],
)
def test_repo_failures_are_not(output: str) -> None:
    assert not NETWORK_ERROR.search(output)
