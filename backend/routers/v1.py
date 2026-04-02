from __future__ import annotations

from fastapi import APIRouter

from backend.routers import ingestion, library, matching, playlists, stations

router = APIRouter(prefix="/api/v1")
router.include_router(stations.router, prefix="/stations", tags=["stations"])
router.include_router(ingestion.router, prefix="/ingestion", tags=["ingestion"])
router.include_router(library.router, prefix="/library", tags=["library"])
router.include_router(playlists.router, prefix="/playlists", tags=["playlists"])
router.include_router(matching.router, prefix="/matching", tags=["matching"])
