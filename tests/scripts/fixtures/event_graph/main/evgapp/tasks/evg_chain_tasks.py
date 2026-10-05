from ..evg_enqueue_chain import enqueue_or_log
from ..evg_huey_app import huey


@huey.task()
def chain_head_task(batch_id: str) -> None:
    enqueue_or_log(lambda: chain_mid_task(batch_id), task_name="chain_mid_task")


@huey.task()
def chain_mid_task(batch_id: str) -> None:
    enqueue_or_log(chain_tail_task, task_name="chain_tail_task")


@huey.task()
def chain_tail_task() -> None:
    return None


@huey.task()
def unguarded_head_task(batch_id: str) -> None:
    chain_tail_task()


@huey.task()
def stray_head_task(batch_id: str) -> None:
    chain_mid_task(batch_id)


@huey.task()
def fan_out_task(batch_id: str) -> None:
    enqueue_or_log(lambda: chain_mid_task(batch_id), task_name="chain_mid_task")
    enqueue_or_log(chain_tail_task, task_name="chain_tail_task")
    enqueue_or_log(chain_tail_task, task_name="chain_tail_task")


@huey.task()
def cyc_a_task(n: int) -> None:
    enqueue_or_log(lambda: cyc_b_task(n), task_name="cyc_b_task")


@huey.task()
def cyc_b_task(n: int) -> None:
    enqueue_or_log(lambda: cyc_a_task(n - 1), task_name="cyc_a_task")


@huey.task()
def table_only_task() -> None:
    return None
