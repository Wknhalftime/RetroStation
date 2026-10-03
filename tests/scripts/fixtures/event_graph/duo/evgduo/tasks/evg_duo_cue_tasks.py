from ..evg_duo_cue_app import cue


@cue.task()
def cue_task(file_id: str) -> None:
    return None
