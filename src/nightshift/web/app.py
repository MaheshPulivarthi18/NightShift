"""NightShift web server: the JSON API (/api) and the React app built from frontend/ (served at /).

Local by default (`nightshift serve` binds 127.0.0.1) and without login, so do not expose it
publicly. Every call that changes state is guarded in web/common.py.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from nightshift.config import get_settings
from nightshift.events import mark_interrupted_runs
from nightshift.sandbox.docker import remove_orphans
from nightshift.web.api import router as api_router
from nightshift.web.compare_api import router as compare_router
from nightshift.web.record_api import router as record_router
from nightshift.web.tasks_api import router as tasks_router
from nightshift.web.workspace_api import router as workspace_router
from nightshift.worker.sync import sync_loop
from nightshift.worker.task import mark_stale_runs

log = structlog.get_logger()

APP_DIR = Path(__file__).parent / "static" / "app"  # built with `cd frontend && npm install && npm run build`
SOURCE_DIR = Path(__file__).resolve().parents[3]  # the checkout this server runs from: src/nightshift/web -> root
NOT_BUILT = "The NightShift app is not built yet. Run: cd frontend && npm install && npm run build\n"


def checkout_commit(folder: Path = SOURCE_DIR) -> str | None:
    """The commit the checkout is at now, or None outside a git checkout."""
    try:
        out = subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=5, check=False)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


RUNNING = checkout_commit()  # the code this server loaded; a pull or merge after it needs a restart
_latest: tuple[float, str | None] = (0.0, None)


async def _tidy_up() -> None:
    """Runs that died while the server was down: their tasks say so, and their sandboxes go."""
    with suppress(Exception):  # no database or no Docker yet: the app still starts
        stale = await mark_stale_runs()
        if stale:
            log.warning("stale_runs_marked", tasks=stale)
        interrupted = await mark_interrupted_runs()  # setups and other jobs the last server left running
        if interrupted:
            log.warning("interrupted_runs_marked", runs=interrupted)
    with suppress(Exception):
        await remove_orphans(older_than=(get_settings().task_minutes + 15) * 60)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    tidy = asyncio.create_task(_tidy_up())  # in the background: startup never waits on Docker
    syncing = asyncio.create_task(sync_loop())  # your published PRs' feedback, now and every sync_minutes
    yield
    tidy.cancel()
    syncing.cancel()


app = FastAPI(title="NightShift", lifespan=lifespan)
app.include_router(api_router)
app.include_router(tasks_router)
app.include_router(workspace_router)
app.include_router(compare_router)
app.include_router(record_router)
if (APP_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=APP_DIR / "assets"), name="assets")


@app.get("/", response_class=HTMLResponse, response_model=None)
async def index() -> HTMLResponse | PlainTextResponse:
    page = APP_DIR / "index.html"
    if not page.exists():
        return PlainTextResponse(NOT_BUILT, status_code=503)
    # always asked for again: an updated build names new asset files, and a cached page would load the old ones
    return HTMLResponse(page.read_text(), headers={"Cache-Control": "no-cache"})


@app.get("/api/version")
async def version() -> dict[str, object]:
    """The commit the server runs and the one its checkout is at now: when they differ, the page asks for a
    restart (changes need one, and nothing else shows it)."""
    global _latest
    if time.monotonic() - _latest[0] > 30:
        _latest = (time.monotonic(), await asyncio.to_thread(checkout_commit))
    latest = _latest[1]
    return {"running": RUNNING, "latest": latest, "old": bool(RUNNING and latest and latest != RUNNING)}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}
