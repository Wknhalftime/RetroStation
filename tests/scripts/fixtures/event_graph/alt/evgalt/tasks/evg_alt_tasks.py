from ..evg_alt_app import huey


@huey.task()
def alt_task(n: int) -> None:
    return None
