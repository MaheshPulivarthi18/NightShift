"""Findings from benchmarking the PR checker on awslabs/mcp."""

from __future__ import annotations

import pytest

from nightshift.github import ADVISORY_JOB
from nightshift.worker.prcheck import CANNOT_RUN


@pytest.mark.parametrize(
    "name",
    ["Close Open Pending Releases", "release-please", "Labeler", "Pull Request Labeler", "triage", "Mark stale issues",
     "Welcome new contributors", "Auto Assign"],
)  # fmt: skip
def test_housekeeping_jobs_are_advisory(name: str) -> None:
    assert ADVISORY_JOB.search(name)


@pytest.mark.parametrize("name", ["Precommit", "build (3.12)", "Run tests", "lint", "CodeQL", "Release build check"])
def test_code_checks_are_not(name: str) -> None:
    assert not ADVISORY_JOB.search(name)


def test_a_runner_that_cannot_start_could_not_run() -> None:
    assert CANNOT_RUN.search("error: Failed to spawn: `pytest`\n  Caused by: No such file or directory")
