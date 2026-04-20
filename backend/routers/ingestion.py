from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status

from backend.dependencies import get_current_token
from backend.tasks.ingestion_tasks import ingestion_task

router = APIRouter()

Token = Annotated[str, Depends(get_current_token)]

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB


@router.post("/playlists", status_code=status.HTTP_202_ACCEPTED)
async def upload_playlist(
    _token: Token,
    file: UploadFile,
    station_id: str = Form(...),
) -> dict[str, str]:
    """Upload a CSV playlist file for ingestion."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    if file.size and file.size > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")
    file_bytes = await file.read()
    if len(file_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    task_id = uuid.uuid4().hex
    ingestion_task(file_bytes, file.filename, station_id, task_id)

    return {
        "status": "accepted",
        "task_id": task_id,
        "message": f"Ingestion queued for {file.filename}",
    }
