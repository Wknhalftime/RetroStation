from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .tasks.evg_misc_tasks import service_heavy_task


@asynccontextmanager
async def lifespan(app: object) -> AsyncIterator[None]:
    service_heavy_task()
    yield
