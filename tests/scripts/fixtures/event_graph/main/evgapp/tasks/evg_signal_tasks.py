from huey.signals import SIGNAL_COMPLETE, SIGNAL_ERROR

from ..evg_huey_app import huey


@huey.signal(SIGNAL_COMPLETE, SIGNAL_ERROR)
def on_task_done(signal: str, task: object, exc: Exception | None = None) -> None:
    print(signal, task, exc)
