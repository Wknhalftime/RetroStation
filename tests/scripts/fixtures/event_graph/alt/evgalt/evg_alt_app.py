from huey import SqliteHuey

from .tasks import evg_alt_tasks

__all__ = ["evg_alt_tasks", "huey"]

huey = SqliteHuey(filename="evg_alt.db")
