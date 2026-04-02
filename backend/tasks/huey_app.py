from huey import SqliteHuey  # type: ignore[import-untyped]

huey = SqliteHuey(filename="huey.db", results=True)
