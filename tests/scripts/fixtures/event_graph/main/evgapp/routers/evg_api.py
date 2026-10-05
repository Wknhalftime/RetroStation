from collections.abc import Callable

from ..evg_support import Track
from ..services.evg_catalog_service import refresh_catalog
from ..tasks import evg_chain_tasks
from ..tasks.evg_chain_tasks import (
    chain_head_task,
    chain_tail_task,
    fan_out_task,
    stray_head_task,
    table_only_task,
)
from ..tasks.evg_misc_tasks import (
    fake_guarded_task,
    own_envelope_task,
    run_envelope_task,
    telemetry_task,
    typed_task,
)

TASK_TABLE = {"head": chain_head_task, "only": table_only_task}


def start_batch(batch_id: str) -> None:
    chain_head_task(batch_id)


def publish_batch_started(batch_id: str) -> None:
    chain_head_task(batch_id)


def start_batch_via_module(batch_id: str) -> None:
    evg_chain_tasks.unguarded_head_task(batch_id)


def start_stray(batch_id: str) -> None:
    stray_head_task(batch_id)


def start_fan_out(batch_id: str) -> None:
    fan_out_task(batch_id)


def wait_for_tail() -> object:
    handle = chain_tail_task()
    return handle.get(blocking=True)


def enqueue_and_read_options(options: dict[str, str]) -> str:
    handle = chain_tail_task()
    mode = options.get("mode", "fast")
    return f"{mode}:{handle.id}"


def schedule_tail() -> None:
    chain_tail_task.schedule(delay=60)


def ingest(item_id: int, record: Track) -> None:
    typed_task(item_id, ["a"], {"n": 1}, None, b"", record, [["x"]])


def kick_off_maintenance() -> None:
    run_envelope_task()
    telemetry_task()
    own_envelope_task()
    fake_guarded_task()


def rebuild_catalog() -> None:
    refresh_catalog()


def dispatch_by_name(name: str) -> None:
    getattr(evg_chain_tasks, name)()


def run_in_thread(fn: Callable[[str], object]) -> None:
    fn("x")


def hand_over() -> None:
    run_in_thread(chain_head_task)
