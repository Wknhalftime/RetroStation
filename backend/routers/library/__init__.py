from __future__ import annotations

from fastapi import APIRouter

from backend.routers.library import artists, scan, status, works

router = APIRouter()
router.include_router(scan.router)
router.include_router(status.router)
router.include_router(artists.router)
router.include_router(works.router)
