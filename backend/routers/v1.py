from __future__ import annotations

from fastapi import APIRouter

from backend.routers import ingestion

router = APIRouter(prefix="/api/v1")
router.include_router(ingestion.router, prefix="/ingestion", tags=["ingestion"])
