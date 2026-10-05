from huey import SqliteHuey

from .tasks import evg_duo_lib_tasks, evg_duo_misrouted_tasks

__all__ = ["evg_duo_lib_tasks", "evg_duo_misrouted_tasks", "huey"]

huey = SqliteHuey(filename="evg_duo.db", results=False)
