from collections.abc import Callable


def enqueue_or_log(enqueue: Callable[[], object], *, task_name: str) -> None:
    try:
        enqueue()
    except OSError:
        print(task_name)
