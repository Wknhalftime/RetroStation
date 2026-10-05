from ..evg_duo_main_app import huey


@huey.task()
def lib_task() -> None:
    return None
