from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus


class BroadcastArtistRepository(ABC):
    @abstractmethod
    def upsert(self, artist: BroadcastArtist) -> BroadcastArtist:
        """Insert or ignore on normalized_name conflict. Always returns the stored row."""
        ...

    @abstractmethod
    def get_by_id(self, artist_id: UUID) -> BroadcastArtist | None: ...

    @abstractmethod
    def get_by_normalized_name(self, normalized_name: str) -> BroadcastArtist | None: ...

    @abstractmethod
    def get_all_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
        """All artists linked to this playlist's events, regardless of match status."""
        ...

    @abstractmethod
    def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
        """Artists linked to this playlist's events with match_status=PENDING."""
        ...

    @abstractmethod
    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
        """Artists linked to this playlist's events with embedding IS NULL."""
        ...

    @abstractmethod
    def update_match_status(self, artist_id: UUID, status: MatchStatus) -> None:
        """Update match status. The artist table has no tier column; tier is
        recorded only on the match row itself, which the caller creates separately."""
        ...

    @abstractmethod
    def update_embedding(self, artist_id: UUID, embedding: list[float]) -> None: ...
