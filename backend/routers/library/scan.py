from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from backend.config import get_settings
from backend.dependencies import get_current_token
from backend.tasks.library_scan_tasks import library_scan_task

router = APIRouter()

Token = Annotated[str, Depends(get_current_token)]


class ScanRequest(BaseModel):
    root_path: str


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_library(body: ScanRequest, _token: Token) -> dict[str, str]:
    """Enqueue a background library scan for the given directory."""
    scan_path = Path(body.root_path).resolve()

    settings = get_settings()
    allowed = [Path(p).resolve() for p in settings.library_scan_paths]
    if allowed and not any(
        scan_path == p or scan_path.is_relative_to(p) for p in allowed
    ):
        raise HTTPException(
            status_code=403,
            detail="Path not in allowed scan paths",
        )

    if not scan_path.exists() or not scan_path.is_dir():
        raise HTTPException(status_code=400, detail="Invalid directory path")

    library_scan_task(body.root_path)
    return {"status": "accepted", "message": f"Library scan queued for {body.root_path}"}
