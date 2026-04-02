from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel

from backend.tasks.library_tasks import library_scan_task

router = APIRouter()


class ScanRequest(BaseModel):
    root_path: str


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_library(body: ScanRequest) -> dict[str, str]:
    """Enqueue a background library scan for the given directory."""
    library_scan_task(body.root_path)
    return {"status": "accepted", "message": f"Library scan queued for {body.root_path}"}
