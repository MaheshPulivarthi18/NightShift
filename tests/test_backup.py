"""A database dump before anything destructive."""

from __future__ import annotations

import pytest

from nightshift.backup import BackupError, dump_command

URL = "postgresql+psycopg://nightshift:secret@localhost:5432/nightshift"


def test_the_local_pg_dump_is_used_when_installed() -> None:
    assert dump_command(URL, True, None) == [
        "pg_dump", "--format=custom", "--no-owner", "-U", "nightshift", "-d", "nightshift",
        "-h", "localhost", "-p", "5432",
    ]  # fmt: skip


def test_otherwise_the_one_in_the_postgres_container() -> None:
    command = dump_command(URL, False, "nightshift-postgres-1")
    assert command[:5] == ["docker", "exec", "-e", "PGPASSWORD=secret", "nightshift-postgres-1"]
    assert command[5:] == ["pg_dump", "--format=custom", "--no-owner", "-U", "nightshift", "-d", "nightshift"]
    with pytest.raises(BackupError, match="No pg_dump"):
        dump_command(URL, False, None)
