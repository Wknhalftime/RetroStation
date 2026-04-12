from __future__ import annotations

from abc import ABC, abstractmethod

from backend.domain.system import UserSetting


class UserSettingRepository(ABC):
    @abstractmethod
    def get(self, key: str) -> UserSetting | None: ...

    @abstractmethod
    def upsert(self, setting: UserSetting) -> UserSetting: ...

    @abstractmethod
    def list_all(self) -> list[UserSetting]: ...

