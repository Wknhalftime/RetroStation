from huey import SqliteHuey  # type: ignore[import-untyped]

# Single-worker SQLite backend; sufficient for this single-user tool.
# Replace with RedisHuey for multi-worker or multi-user deployments.
huey = SqliteHuey(filename="huey.db", results=True)

# Import all task modules so they register with the Huey consumer.
# Without these imports, the worker cannot deserialize queued tasks.
import backend.tasks.artist_matching_tasks  # noqa: F401, E402
import backend.tasks.embedding_tasks  # noqa: F401, E402
import backend.tasks.identity_matching_tasks  # noqa: F401, E402
import backend.tasks.ingestion_tasks  # noqa: F401, E402
import backend.tasks.library_enrichment_tasks  # noqa: F401, E402
import backend.tasks.library_tasks  # noqa: F401, E402
import backend.tasks.mb_enrichment_tasks  # noqa: F401, E402
