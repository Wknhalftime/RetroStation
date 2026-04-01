from uuid import UUID
from backend.domain.models import LogEvent
from backend.repositories.log_events import LogEventRepository


class FakeLogEventRepository(LogEventRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LogEvent] = {}

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
