"""The track record page: what happened to the pull requests made with NightShift (worker/record.py)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from nightshift.web.common import Guard
from nightshift.worker import record

router = APIRouter(prefix="/api")


@router.get("/track-record")
async def track_record() -> dict[str, Any]:
    return await record.track_record()


class AddPR(BaseModel):
    url: str = Field(max_length=300)


@router.post("/track-record", dependencies=[Guard])
async def add_pr(body: AddPR) -> dict[str, Any]:
    """Count a pull request of yours made before NightShift kept its task, or opened by hand."""
    try:
        await record.add_pr(body.url)
    except record.RecordError as e:
        raise HTTPException(409, str(e)) from e
    return await record.track_record()
