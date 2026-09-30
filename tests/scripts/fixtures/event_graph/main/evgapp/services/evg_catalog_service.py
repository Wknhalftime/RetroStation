def refresh_catalog() -> None:
    return None


def count_pending() -> int:
    return 0


def load_rows() -> list[str]:
    return []


def kick_tail() -> None:
    from ..tasks.evg_chain_tasks import chain_tail_task

    chain_tail_task()
