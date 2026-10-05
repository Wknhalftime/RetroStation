from ..tasks.evg_chain_tasks import chain_tail_task


def check_tail() -> None:
    chain_tail_task.call_local()
