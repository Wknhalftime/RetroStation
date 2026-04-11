from huey import SqliteHuey  # type: ignore[import-untyped]

from backend.config import get_settings
from backend.logging_config import configure_logging

# Single-worker SQLite backend (WAL mode); sufficient for this single-user tool.
# results=False because all tasks are fire-and-forget (no .get() calls).
# Replace with RedisHuey for multi-worker or multi-user deployments.
huey = SqliteHuey(filename="huey.db", results=False)

# Ensure structured logging is configured in the worker process.
# The FastAPI server calls configure_logging() in its lifespan, but the
# Huey consumer is a separate process that never imports backend.main.
configure_logging(get_settings().log_level)

# Import all task modules so they register with the Huey consumer.
# Without these imports, the worker cannot deserialize queued tasks.
import backend.tasks.artist_matching_tasks  # noqa: F401, E402
import backend.tasks.embedding_tasks  # noqa: F401, E402
import backend.tasks.identity_matching_tasks  # noqa: F401, E402
import backend.tasks.ingestion_tasks  # noqa: F401, E402
import backend.tasks.library_enrichment_tasks  # noqa: F401, E402
import backend.tasks.library_tasks  # noqa: F401, E402
import backend.tasks.library_watcher_tasks  # noqa: F401, E402
import backend.tasks.mb_enrichment_tasks  # noqa: F401, E402
import backend.tasks.normalize_backfill_task  # noqa: F401, E402
