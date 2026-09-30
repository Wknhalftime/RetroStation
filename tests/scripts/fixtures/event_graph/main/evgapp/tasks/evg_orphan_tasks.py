from ..evg_huey_app import huey


@huey.task()
def orphan_task() -> None:
    return None
