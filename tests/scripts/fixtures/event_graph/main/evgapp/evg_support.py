from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class Track:
    title: str


@contextmanager
def task_run(name: str) -> Iterator[None]:
    yield


@contextmanager
def task_failure_telemetry(name: str) -> Iterator[None]:
    yield


def enqueue_or_log(enqueue: Callable[[], object], *, task_name: str) -> None:
    enqueue()


def chain_tail_task() -> None:
    return None


def local_call() -> None:
    chain_tail_task()
