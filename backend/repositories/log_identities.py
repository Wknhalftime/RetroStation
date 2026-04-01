from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LogIdentity


class LogIdentityRepository(ABC):
    @abstractmethod
    def upsert(self, identity: LogIdentity) -> LogIdentity:
        """Insert or ignore on normalized_signature conflict. Always returns stored row."""
        ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> LogIdentity | None: ...

    @abstractmethod
    def get_by_signature(self, normalized_signature: str) -> LogIdentity | None: ...

    @abstractmethod
    def get_for_artist(self, artist_id: UUID) -> list[LogIdentity]: ...

    @abstractmethod
    def get_pending_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        """Identities linked to this playlist's events with match_status=PENDING
        and their log_artist already resolved."""
        ...

    @abstractmethod
    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        """Identities linked to this playlist's events with embedding IS NULL."""
        ...

    @abstractmethod
    def update_match_status(
        self,
        id: UUID,
        status: MatchStatus,
        tier: MatchTier,
    ) -> None: ...

    @abstractmethod
    def update_embedding(self, id: UUID, embedding: list[float]) -> None: ...

    @abstractmethod
    def bulk_reject_by_artist(self, artist_id: UUID) -> None:
        """Set all identities for this artist to AUTO_REJECTED."""
        ...
