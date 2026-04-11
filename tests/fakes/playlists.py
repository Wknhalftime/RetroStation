from uuid import UUID

from backend.domain.broadcast import Playlist
from backend.repositories.playlists import PlaylistRepository


class FakePlaylistRepository(PlaylistRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, Playlist] = {}

    def create(self, playlist: Playlist) -> Playlist:
        self._data[playlist.id] = playlist
        return playlist

    def get_by_id(self, id: UUID) -> Playlist | None:
        return self._data.get(id)

    def get_by_content_hash(self, content_hash: str) -> Playlist | None:
        return next((p for p in self._data.values() if p.content_hash == content_hash), None)

    def list_by_station(self, station_id: UUID) -> list[Playlist]:
        return [p for p in self._data.values() if p.station_id == station_id]
