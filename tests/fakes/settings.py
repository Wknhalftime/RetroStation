from backend.repositories.settings import SettingsRepository


class FakeSettingsRepository(SettingsRepository):
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = initial or {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def get_all(self) -> dict[str, str]:
        return dict(self._data)
