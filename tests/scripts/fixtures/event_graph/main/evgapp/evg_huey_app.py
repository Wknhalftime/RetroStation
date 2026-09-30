from huey import SqliteHuey

from .tasks import evg_chain_tasks, evg_misc_tasks, evg_signal_tasks

__all__ = ["evg_chain_tasks", "evg_misc_tasks", "evg_signal_tasks", "huey"]

huey = SqliteHuey(filename="evg.db", results=False)
