from huey import SqliteHuey

from .tasks import evg_duo_cue_tasks

__all__ = ["cue", "evg_duo_cue_tasks"]

cue = SqliteHuey(filename="evg_duo_cue.db", results=False)
