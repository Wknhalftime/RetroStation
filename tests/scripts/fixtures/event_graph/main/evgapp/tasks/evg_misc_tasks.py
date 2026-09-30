from huey import crontab

from ..evg_huey_app import huey
from ..evg_support import Track, enqueue_or_log, task_failure_telemetry, task_run
from ..services.evg_catalog_service import count_pending, load_rows, refresh_catalog
from .evg_chain_tasks import chain_tail_task

ENABLED = True


def _log(rows: list[str]) -> None:
    print(len(rows))


@huey.periodic_task(crontab(minute="*/4"))
def sweep_periodic() -> None:
    pending = count_pending()
    if pending:
        refresh_catalog()


@huey.periodic_task(crontab(minute="0"))
def heartbeat_periodic() -> None:
    if ENABLED:
        refresh_catalog()


@huey.periodic_task(crontab(minute="*/5"))
def resume_periodic() -> None:
    chain_tail_task.call_local()


@huey.task()
def typed_task(
    item_id: int,
    tags: list[str],
    opts: dict[str, int],
    label: str | None,
    blob: bytes,
    record: Track,
    nested: list[list[str]],
) -> None:
    return None


@huey.task()
def service_heavy_task() -> None:
    rows = load_rows()
    refresh_catalog()
    refresh_catalog()
    _log(rows)
    count_pending()


@huey.task()
def run_envelope_task() -> None:
    with task_run("run"):
        refresh_catalog()


@huey.task()
def telemetry_task() -> None:
    with task_failure_telemetry("telemetry"):
        refresh_catalog()


@huey.task(retries=2)
def own_envelope_task() -> None:
    try:
        refresh_catalog()
    except OSError:
        _log([])


@huey.task()
def fake_guarded_task() -> None:
    enqueue_or_log(lambda: chain_tail_task(), task_name="chain_tail_task")
