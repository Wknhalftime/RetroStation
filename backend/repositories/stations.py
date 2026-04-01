from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import Station


class StationRepository(ABC):
    @abstractmethod
    def create(self, station: Station) -> Station: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> Station | None: ...

    @abstractmethod
    def get_by_call_letters(self, call_letters: str) -> Station | None: ...

    @abstractmethod
    def list_all(self) -> list[Station]: ...

    @abstractmethod
    def update(self, station: Station) -> Station: ...

    @abstractmethod
    def delete(self, id: UUID) -> None: ...
