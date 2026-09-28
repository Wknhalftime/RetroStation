import os

from huey import SqliteHuey  # type: ignore[import-untyped]

from backend.config import get_settings
from backend.logging_config import configure_logging

# Per-worker file under pytest-xdist so parallel workers don't collide on the
# same SQLite lock. In prod PYTEST_XDIST_WORKER is unset → "huey.db" as before.
_worker = os.environ.get("PYTEST_XDIST_WORKER")
_huey_filename = f"huey_{_worker}.db" if _worker else "huey.db"

# Single-worker SQLite backend (WAL mode); sufficient for this single-user tool.
# results=False because all tasks are fire-and-forget (no .get() calls).
# Replace with RedisHuey for multi-worker or multi-user deployments.
huey = SqliteHuey(filename=_huey_filename, results=False)


@huey.on_startup()  # type: ignore[untyped-decorator]
def configure_consumer_logging() -> None:
    """Send the consumer's structlog events to stdout and ``system_logs``.

    The FastAPI server calls configure_logging() in its lifespan, but the
    Huey consumer is a separate process that never imports backend.main.
    Huey runs this in each worker before its first task. A startup hook, not
    an import side effect, so importing a task module (tests, bench scripts)
    leaves the process's logging alone.
    """
    settings = get_settings()
    configure_logging(settings.log_level, database_url=settings.database_url)


# Import all task modules so they register with the Huey consumer.
# Without these imports, the worker cannot deserialize queued tasks.
import backend.tasks.artist_matching_tasks  # noqa: F401, E402
import backend.tasks.embedding_tasks  # noqa: F401, E402
import backend.tasks.identity_matching_tasks  # noqa: F401, E402
import backend.tasks.ingestion_tasks  # noqa: F401, E402
import backend.tasks.library_enrichment_tasks  # noqa: F401, E402
import backend.tasks.library_hash_backfill_tasks  # noqa: F401, E402
import backend.tasks.library_scan_tasks  # noqa: F401, E402
import backend.tasks.library_watcher_tasks  # noqa: F401, E402
import backend.tasks.mb_enrichment_tasks  # noqa: F401, E402
import backend.tasks.normalize_backfill_tasks  # noqa: F401, E402
