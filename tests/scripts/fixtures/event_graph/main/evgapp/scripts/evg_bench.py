from ..tasks.evg_chain_tasks import chain_tail_task


def bench() -> None:
    chain_tail_task.call_local()


def bench_raw() -> None:
    chain_tail_task.func()
