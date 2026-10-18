from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.broadcast import BroadcastStation
from backend.domain.liners import LinerCadence


class BroadcastStationRepository(ABC):
    @abstractmethod
    def create(self, station: BroadcastStation) -> BroadcastStation:
        """Raises `DuplicateCallLettersError` when another station has these call letters
        in any case (D72)."""
        ...

    @abstractmethod
    def get_by_id(self, station_id: UUID) -> BroadcastStation | None: ...

    @abstractmethod
    def get_by_call_letters(self, call_letters: str) -> BroadcastStation | None: ...

    @abstractmethod
    def list_all(self) -> list[BroadcastStation]: ...

    @abstractmethod
    def update(self, station: BroadcastStation) -> BroadcastStation:
        """Store the station's details; its liner cadence stays as it is (only
        ``set_liner_cadence`` changes it). Raises `DuplicateCallLettersError` when another
        station has these call letters in any case (D72)."""
        ...

    @abstractmethod
    def set_liner_cadence(self, station_id: UUID, cadence: LinerCadence | None) -> None:
        """Set the station's liner cadence (LR1); None turns its liners off."""
        ...

    @abstractmethod
    def delete(self, station_id: UUID) -> None: ...
