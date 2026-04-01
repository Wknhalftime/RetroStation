from abc import ABC, abstractmethod

from backend.domain.models import UserSetting


class SettingsRepository(ABC):
    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def set(self, key: str, value: str) -> None: ...

    @abstractmethod
    def get_all(self) -> dict[str, str]: ...
