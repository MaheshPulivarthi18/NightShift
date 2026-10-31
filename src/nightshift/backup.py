"""A dump of the database before anything destructive, so a purge has an undo.

`pg_dump` in Postgres' custom format (restore with `pg_restore -d <url> <file>`), into `backups_dir`. The
local `pg_dump` is used when installed; otherwise the one inside the Postgres container that serves the
database's port (docker compose's `postgres`), so nothing needs installing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from nightshift.config import get_settings


class BackupError(RuntimeError):
    pass


def _container(port: int) -> str | None:
    """The running container that publishes `port`, if any."""
    try:
        out = subprocess.run(["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=20, check=False)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    names = out.stdout.split()
    return names[0] if names else None


def dump_command(database_url: str, local: bool, container: str | None) -> list[str]:
    """The pg_dump command line for the database at `database_url`."""
    url = make_url(database_url)
    args = ["pg_dump", "--format=custom", "--no-owner", "-U", url.username or "postgres", "-d", url.database or ""]
    if local:
        return [*args, "-h", url.host or "localhost", "-p", str(url.port or 5432)]
    if container:  # inside the container the server is local to it
        return ["docker", "exec", "-e", f"PGPASSWORD={url.password or ''}", container, *args]
    raise BackupError("No pg_dump here and no running Postgres container publishes the database's port.")


def backup(reason: str) -> Path:
    """Dump the whole database to a new file and return its path. Raises BackupError when it cannot."""
    settings = get_settings()
    url = make_url(settings.database_url)
    local = shutil.which("pg_dump") is not None
    command = dump_command(settings.database_url, local, None if local else _container(url.port or 5432))
    folder = Path(settings.backups_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() else "-" for c in reason).strip("-")[:40] or "backup"
    path = folder / f"{url.database}-{stamp}-{slug}.dump"
    env = {"PGPASSWORD": url.password or ""} if local else None
    with path.open("wb") as out:
        try:
            done = subprocess.run(command, stdout=out, stderr=subprocess.PIPE, timeout=600, check=False,
                                  env={**os.environ, **env} if env else None)  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as e:
            path.unlink(missing_ok=True)
            raise BackupError(f"pg_dump did not run: {e}") from e
    if done.returncode != 0 or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise BackupError(f"pg_dump failed: {done.stderr.decode(errors='replace').strip()[:300]}")
    return path
