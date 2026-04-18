import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import psycopg
import structlog
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from backend.config import get_settings
from backend.db.migrations import run_migrations
from backend.db.pool import close_pool, init_pool
from backend.logging_config import configure_logging
from backend.routers.v1 import router as v1_router
from backend.websocket import websocket_endpoint

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, database_url=settings.database_url)

    if settings.airwave_token == "dev-token":
        logger.warning(
            "security_warning",
            message="Using default dev-token. Set AIRWAVE_TOKEN in .env for production.",
        )

    pool = init_pool(settings.database_url)
    await pool.open()

    # CRITICAL: RETROSTATION_SKIP_BOOT_MIGRATIONS is a TEST-ONLY escape hatch.
    # Production deployments must never set it. The test harness sets it from
    # tests/routers/conftest.py only, where session-scope fixtures have already
    # applied migrations against the same DB URL.
    if os.getenv("RETROSTATION_SKIP_BOOT_MIGRATIONS") == "1":
        logger.warning(
            "boot_migrations_skipped",
            message="RETROSTATION_SKIP_BOOT_MIGRATIONS=1; skipping lifespan migrations.",
        )
    else:
        with psycopg.connect(settings.database_url) as conn:
            run_migrations(conn)
            conn.commit()

    yield

    await close_pool()


app = FastAPI(title="RetroStation", lifespan=lifespan)

# CORS: allow the Vite dev server to talk to the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time task progress broadcast."""
    await websocket_endpoint(websocket)
