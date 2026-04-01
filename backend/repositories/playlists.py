from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import Playlist


class PlaylistRepository(ABC):
    @abstractmethod
    def create(self, playlist: Playlist) -> Playlist: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> Playlist | None: ...

    @abstractmethod
    def get_by_content_hash(self, content_hash: str) -> Playlist | None: ...

    @abstractmethod
    def list_by_station(self, station_id: UUID) -> list[Playlist]: ...
