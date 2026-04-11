from abc import ABC, abstractmethod
from datetime import date
from uuid import UUID

from backend.domain.broadcast import PlayEvent


class PlayEventRepository(ABC):
    @abstractmethod
    def create(self, event: PlayEvent) -> PlayEvent:
        """Insert or ignore on (identity_id, playlist_id, played_at) conflict."""
        ...

    @abstractmethod
    def get_by_playlist(self, playlist_id: UUID) -> list[PlayEvent]: ...

    @abstractmethod
    def get_by_identity(self, identity_id: UUID) -> list[PlayEvent]: ...

    @abstractmethod
    def get_by_station_date(self, station_id: UUID, broadcast_date: date) -> list[PlayEvent]: ...
