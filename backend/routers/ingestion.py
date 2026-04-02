from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, UploadFile, status

from backend.tasks.ingestion_tasks import ingestion_task

router = APIRouter()


@router.post("/playlists", status_code=status.HTTP_202_ACCEPTED)
async def upload_playlist(
    file: UploadFile,
    station_id: str = Form(...),
) -> dict[str, str]:
    """Upload a CSV playlist file for ingestion."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    # Enqueue the task (fire-and-forget)
    ingestion_task(file_bytes, file.filename, station_id)

    return {"status": "accepted", "message": f"Ingestion queued for {file.filename}"}
