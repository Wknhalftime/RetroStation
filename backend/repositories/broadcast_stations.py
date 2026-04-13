from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.broadcast import BroadcastStation


class BroadcastStationRepository(ABC):
    @abstractmethod
    def create(self, station: BroadcastStation) -> BroadcastStation: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> BroadcastStation | None: ...

    @abstractmethod
    def get_by_call_letters(self, call_letters: str) -> BroadcastStation | None: ...

    @abstractmethod
    def list_all(self) -> list[BroadcastStation]: ...

    @abstractmethod
    def update(self, station: BroadcastStation) -> BroadcastStation: ...

    @abstractmethod
    def delete(self, id: UUID) -> None: ...

