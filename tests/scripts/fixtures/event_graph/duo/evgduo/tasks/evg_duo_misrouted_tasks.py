from ..evg_duo_cue_app import cue


@cue.task()
def misrouted_task() -> None:
    return None
