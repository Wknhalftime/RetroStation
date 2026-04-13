from datetime import date
from uuid import UUID

from backend.domain.broadcast import BroadcastPlayEvent
from backend.repositories.broadcast_play_events import BroadcastPlayEventRepository


class FakeBroadcastPlayEventRepository(BroadcastPlayEventRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastPlayEvent] = {}
        self._playlist_station_map: dict[UUID, UUID] = {}

    def set_playlist_station(
        self, playlist_id: UUID, station_id: UUID
    ) -> None:
        """Test helper: register which station a playlist belongs to."""
        self._playlist_station_map[playlist_id] = station_id

    def create(self, event: BroadcastPlayEvent) -> BroadcastPlayEvent:
        key = (event.identity_id, event.playlist_id, event.played_at)
        existing = next(
            (e for e in self._data.values()
             if (e.identity_id, e.playlist_id, e.played_at) == key), None
        )
        if existing:
            return existing
        self._data[event.id] = event
        return event

    def get_by_playlist(self, playlist_id: UUID) -> list[BroadcastPlayEvent]:
        return [
            e for e in self._data.values()
            if e.playlist_id == playlist_id
        ]

    def get_by_identity(self, identity_id: UUID) -> list[BroadcastPlayEvent]:
        return [
            e for e in self._data.values()
            if e.identity_id == identity_id
        ]

    def get_by_station_date(
        self, station_id: UUID, broadcast_date: date
    ) -> list[BroadcastPlayEvent]:
        playlist_ids = {
            pid for pid, sid in self._playlist_station_map.items()
            if sid == station_id
        }
        return [
            e for e in self._data.values()
            if e.playlist_id in playlist_ids
            and e.played_at.date() == broadcast_date
        ]

