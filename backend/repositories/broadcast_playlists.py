from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.broadcast import BroadcastPlaylist


class BroadcastPlaylistRepository(ABC):
    @abstractmethod
    def create(self, playlist: BroadcastPlaylist) -> BroadcastPlaylist: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> BroadcastPlaylist | None: ...

    @abstractmethod
    def get_by_content_hash(self, content_hash: str) -> BroadcastPlaylist | None: ...

    @abstractmethod
    def list_by_station(self, station_id: UUID) -> list[BroadcastPlaylist]: ...

