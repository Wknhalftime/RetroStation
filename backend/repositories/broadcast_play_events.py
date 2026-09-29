from abc import ABC, abstractmethod
from datetime import date
from uuid import UUID

from backend.domain.broadcast import BroadcastPlayEvent


class BroadcastPlayEventRepository(ABC):
    @abstractmethod
    def create(self, event: BroadcastPlayEvent) -> BroadcastPlayEvent:
        """Insert or ignore on (identity_id, playlist_id, played_at) conflict."""
        ...

    @abstractmethod
    def get_by_playlist(self, playlist_id: UUID) -> list[BroadcastPlayEvent]: ...

    @abstractmethod
    def get_by_identity(self, identity_id: UUID) -> list[BroadcastPlayEvent]: ...

    @abstractmethod
    def get_by_station_date(
        self, station_id: UUID, broadcast_date: date
    ) -> list[BroadcastPlayEvent]:
        """The station's plays logged on ``broadcast_date`` (the stored wall-clock date), in
        ``played_at``, identity id, event id order (spec D4, D19)."""
        ...
