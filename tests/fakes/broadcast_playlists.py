from uuid import UUID

from backend.domain.broadcast import BroadcastPlaylist
from backend.repositories.broadcast_playlists import BroadcastPlaylistRepository


class FakeBroadcastPlaylistRepository(BroadcastPlaylistRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastPlaylist] = {}

    def create(self, playlist: BroadcastPlaylist) -> BroadcastPlaylist:
        self._data[playlist.id] = playlist
        return playlist

    def get_by_id(self, playlist_id: UUID) -> BroadcastPlaylist | None:
        return self._data.get(playlist_id)

    def get_by_content_hash(self, content_hash: str) -> BroadcastPlaylist | None:
        return next((p for p in self._data.values() if p.content_hash == content_hash), None)

    def list_by_station(self, station_id: UUID) -> list[BroadcastPlaylist]:
        return [p for p in self._data.values() if p.station_id == station_id]

