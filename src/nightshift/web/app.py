"""NightShift web server: the JSON API (/api) and the React app built from frontend/ (served at /).

Local by default (`nightshift serve` binds 127.0.0.1) and without login, so do not expose it
publicly. Every call that changes state is guarded in web/common.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from nightshift.config import get_settings
from nightshift.sandbox.docker import remove_orphans
from nightshift.web.api import router as api_router
from nightshift.web.tasks_api import router as tasks_router
from nightshift.worker.task import mark_stale_runs

log = structlog.get_logger()

APP_DIR = Path(__file__).parent / "static" / "app"  # built with `cd frontend && npm install && npm run build`
NOT_BUILT = "The NightShift app is not built yet. Run: cd frontend && npm install && npm run build\n"


async def _tidy_up() -> None:
    """Runs that died while the server was down: their tasks say so, and their sandboxes go."""
    with suppress(Exception):  # no database or no Docker yet: the app still starts
        stale = await mark_stale_runs()
        if stale:
            log.warning("stale_runs_marked", tasks=stale)
    with suppress(Exception):
        await remove_orphans(older_than=(get_settings().task_minutes + 15) * 60)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    tidy = asyncio.create_task(_tidy_up())  # in the background: startup never waits on Docker
    yield
    tidy.cancel()


app = FastAPI(title="NightShift", lifespan=lifespan)
app.include_router(api_router)
app.include_router(tasks_router)
if (APP_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=APP_DIR / "assets"), name="assets")


@app.get("/", response_class=HTMLResponse, response_model=None)
async def index() -> HTMLResponse | PlainTextResponse:
    page = APP_DIR / "index.html"
    if not page.exists():
        return PlainTextResponse(NOT_BUILT, status_code=503)
    return HTMLResponse(page.read_text())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}
