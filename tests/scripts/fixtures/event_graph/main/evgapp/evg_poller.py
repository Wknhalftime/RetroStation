import time
from collections.abc import Callable


def watch_progress(cur) -> None:
    while True:
        cur.execute("SELECT status FROM progress_rows WHERE done = false")
        if cur.fetchone() is None:
            return
        time.sleep(0.5)


def retry_connect(connect: Callable[[], object]) -> object:
    for attempt in range(3):
        try:
            return connect()
        except OSError:
            time.sleep(attempt)
    raise OSError("no connection")


def bulk_insert(cur, rows: list[str]) -> None:
    for row in rows:
        cur.execute("INSERT INTO items VALUES (%s)", (row,))
