"""Where the Huey queues keep their SQLite files.

A relative file name resolves against each process's working directory, so an API and a
consumer started from different directories would use different queues, with no error. The
files are anchored to the project root instead: every process of one checkout agrees.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def huey_db_path(stem: str) -> str:
    """The absolute path of the queue file named ``stem``, at the project root.

    Under pytest-xdist each worker gets its own file (``<stem>_gw0.db``), so parallel
    workers never contend for one SQLite lock.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    name = f"{stem}_{worker}.db" if worker else f"{stem}.db"
    return str(PROJECT_ROOT / name)
