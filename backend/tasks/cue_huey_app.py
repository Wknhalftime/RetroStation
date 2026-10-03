"""The cue analysis consumer (D50): its own Huey instance, SQLite file and Procfile line
(``-w 1``), apart from the library worker, so analysis never queues behind a scan."""

import os
import sys

import structlog
from huey import SqliteHuey  # type: ignore[import-untyped]

from backend.config import get_settings
from backend.logging_config import configure_logging
from backend.tasks.huey_files import huey_db_path

logger = structlog.get_logger()

# results=False: the tasks are fire-and-forget, as on the library worker.
cue_huey = SqliteHuey(filename=huey_db_path("huey_cues"), results=False)

_consumer_jobs: list[object] = []
"""The kill-on-close job this process is in. Never closed: closing it would end this process
too. The kernel closes it when the process ends, however it ends."""


@cue_huey.on_startup()  # type: ignore[untyped-decorator]
def configure_cue_consumer_logging() -> None:
    """Send the cue consumer's structlog events to stdout and ``system_logs``.

    A startup hook, as on the library worker: importing a task module leaves logging alone.
    """
    settings = get_settings()
    configure_logging(settings.log_level, database_url=settings.database_url)


@cue_huey.on_startup()  # type: ignore[untyped-decorator]
def join_kill_on_close_job() -> None:
    """Put this consumer in a kill-on-close job, so the analyser it runs dies with it.

    A consumer killed hard runs no ``finally``, so ``analyse_batch`` cannot end a hung
    analyser; the job's children inherit it, and the kernel ends them when the consumer's
    handle closes. Windows only, as in ``backend.main``. If Windows refuses the job, the
    consumer still analyses, without that guarantee.
    """
    if sys.platform != "win32":
        return
    # Lazy: windows_job raises ImportError off Windows, and this line is reached only on win32.
    from backend.playout.windows_job import KillOnCloseJob

    try:
        job = KillOnCloseJob()
    except OSError as error:
        logger.warning("cue_consumer_job_refused", error=str(error))
        return
    try:
        job.assign(os.getpid())
    except OSError as error:
        job.close()  # nothing is assigned, so this ends no process
        logger.warning("cue_consumer_job_refused", error=str(error))
        return
    _consumer_jobs.append(job)


# Register the cue tasks with this consumer, and only this one.
import backend.tasks.stream_cue_tasks  # noqa: F401, E402
