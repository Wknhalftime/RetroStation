from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI

from backend.config import get_settings
from backend.db.migrations import run_migrations
from backend.db.pool import close_pool, init_pool
from backend.logging_config import configure_logging
from backend.routers.v1 import router as v1_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    pool = init_pool(settings.database_url)
    await pool.open()

    # Run migrations synchronously before accepting requests
    with psycopg.connect(settings.database_url) as conn:
        run_migrations(conn)
        conn.commit()

    yield

    await close_pool()


app = FastAPI(title="RetroStation", lifespan=lifespan)
app.include_router(v1_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
