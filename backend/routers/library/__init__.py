from __future__ import annotations

from fastapi import APIRouter

from backend.routers.library import (
    artists,
    cues,
    files,
    missing,
    scan,
    silent_files,
    status,
    works,
)

router = APIRouter()
router.include_router(scan.router)
router.include_router(status.router)
router.include_router(artists.router)
router.include_router(files.router)
router.include_router(works.router)
router.include_router(missing.router)
router.include_router(silent_files.router)
router.include_router(cues.router)
