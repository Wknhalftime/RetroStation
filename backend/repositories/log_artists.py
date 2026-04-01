from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LogArtist


class LogArtistRepository(ABC):
    @abstractmethod
    def upsert(self, artist: LogArtist) -> LogArtist:
        """Insert or ignore on normalized_name conflict. Always returns the stored row."""
        ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> LogArtist | None: ...

    @abstractmethod
    def get_by_normalized_name(self, normalized_name: str) -> LogArtist | None: ...

    @abstractmethod
    def get_pending_for_playlist(self, playlist_id: UUID) -> list[LogArtist]:
        """Artists linked to this playlist's events with match_status=PENDING."""
        ...

    @abstractmethod
    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[LogArtist]:
        """Artists linked to this playlist's events with embedding IS NULL."""
        ...

    @abstractmethod
    def update_match_status(
        self,
        id: UUID,
        status: MatchStatus,
        tier: MatchTier | None = None,
    ) -> None: ...

    @abstractmethod
    def update_embedding(self, id: UUID, embedding: list[float]) -> None: ...
