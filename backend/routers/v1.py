from __future__ import annotations

from fastapi import APIRouter

from backend.routers import ingestion, library

router = APIRouter(prefix="/api/v1")
router.include_router(ingestion.router, prefix="/ingestion", tags=["ingestion"])
router.include_router(library.router, prefix="/library", tags=["library"])
