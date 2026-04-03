from datetime import date
from uuid import UUID

from backend.domain.models import LogEvent
from backend.repositories.log_events import LogEventRepository


class FakeLogEventRepository(LogEventRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LogEvent] = {}
        self._playlist_station_map: dict[UUID, UUID] = {}

    def set_playlist_station(self, playlist_id: UUID, station_id: UUID) -> None:
        """Test helper: register which station a playlist belongs to."""
        self._playlist_station_map[playlist_id] = station_id

    def create(self, event: LogEvent) -> LogEvent:
        key = (event.identity_id, event.playlist_id, event.played_at)
        existing = next(
            (e for e in self._data.values()
             if (e.identity_id, e.playlist_id, e.played_at) == key), None
        )
        if existing:
            return existing
        self._data[event.id] = event
        return event

    def get_by_playlist(self, playlist_id: UUID) -> list[LogEvent]:
        return [e for e in self._data.values() if e.playlist_id == playlist_id]

    def get_by_identity(self, identity_id: UUID) -> list[LogEvent]:
        return [e for e in self._data.values() if e.identity_id == identity_id]

    def get_by_station_date(self, station_id: UUID, broadcast_date: date) -> list[LogEvent]:
        playlist_ids = {
            pid for pid, sid in self._playlist_station_map.items()
            if sid == station_id
        }
        return [
            e for e in self._data.values()
            if e.playlist_id in playlist_ids and e.played_at.date() == broadcast_date
        ]
