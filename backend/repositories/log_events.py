from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import LogEvent


class LogEventRepository(ABC):
    @abstractmethod
    def create(self, event: LogEvent) -> LogEvent:
        """Insert or ignore on (identity_id, playlist_id, played_at) conflict."""
        ...

    @abstractmethod
    def get_by_playlist(self, playlist_id: UUID) -> list[LogEvent]: ...

    @abstractmethod
    def get_by_identity(self, identity_id: UUID) -> list[LogEvent]: ...
