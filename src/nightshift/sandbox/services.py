"""Services a repo's tests need, run inside its own sandbox: PostgreSQL and Redis.

Many test suites expect a database next to them (postgres-connector's fixtures connect to 127.0.0.1:5432 as
`postgres`). The Setup agent asks for a service by name; its packages go into the environment image, which
also lists it in /etc/nightshift/services, so every sandbox started from that image (or from a repo image
built on it) starts the service before anything else runs. It listens on 127.0.0.1 inside the container
only, at its usual port, with no password (PostgreSQL trusts local connections, so any password works).
"""

from __future__ import annotations

from typing import Literal

SERVICE_FILE = "/etc/nightshift/services"
Service = Literal["postgres", "redis"]

PACKAGES: dict[str, list[str]] = {"postgres": ["postgresql"], "redis": ["redis-server"]}

# Run as the sandbox's non-root user, every time a sandbox starts; safe to run again.
START: dict[str, str] = {
    "postgres": (
        "PGBIN=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1); D=/tmp/ns-postgres; "
        'if [ ! -s "$D/PG_VERSION" ]; then "$PGBIN/initdb" -D "$D" -U postgres --auth=trust -E UTF8 '
        '>/tmp/ns-postgres-init.log 2>&1; fi; rm -f "$D/postmaster.pid"; '
        '"$PGBIN/pg_ctl" -D "$D" -l /tmp/ns-postgres.log -w -t 60 '
        '-o "-k /tmp -c listen_addresses=127.0.0.1 -p 5432 -c fsync=off -c full_page_writes=off" start'
    ),
    "redis": "redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --save '' --appendonly no",
}

# For the Setup agent: what it gets, so its commands and test environment match.
DESCRIBED = {
    "postgres": "PostgreSQL (latest Debian 12) on 127.0.0.1:5432, superuser `postgres`, any password, "
    "database `postgres`; create others with `createdb -h 127.0.0.1 -U postgres NAME` or let the tests do it",
    "redis": "Redis on 127.0.0.1:6379, no password",
}


def packages_for(services: list[str]) -> list[str]:
    return sorted({p for s in services for p in PACKAGES.get(s, [])})


def start_script(services: list[str]) -> str:
    """One command starting each listed service that NightShift knows, in order."""
    return " && ".join(f"( {START[s]} )" for s in services if s in START)
