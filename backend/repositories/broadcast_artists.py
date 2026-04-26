from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus, ReasonCode


class BroadcastArtistRepository(ABC):
    @abstractmethod
    def upsert(self, artist: BroadcastArtist) -> BroadcastArtist:
        """Insert or ignore on normalized_name conflict. Always returns the stored row."""
        ...

    @abstractmethod
    def get_by_id(self, artist_id: UUID) -> BroadcastArtist | None: ...

    @abstractmethod
    def get_by_ids(self, ids: list[UUID]) -> list[BroadcastArtist]:
        """Batch fetch artists by ID. Missing IDs are silently omitted (not raised).
        Empty input returns empty list without hitting the DB.
        """
        ...

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
    def update_match_status(
        self,
        artist_id: UUID,
        status: MatchStatus,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> None:
        """Update match status plus optional reason metadata.

        The artist table has no tier column; tier is recorded only on the match
        row itself, which the caller creates separately.

        reason_code / reason_detail default to None — existing callers compile
        unchanged. Strategies populate these in PR 3/PR 4. Passing None
        explicitly clears any previously-persisted reason.
        """
        ...

    @abstractmethod
    def update_embedding(self, artist_id: UUID, embedding: list[float]) -> None: ...

    @abstractmethod
    def reset_deferred_by_ids(self, artist_ids: list[UUID]) -> int:
        """Reset NEEDS_REVIEW/DEFERRED_RETRY artists back to PENDING for the
        given artist IDs. Returns rows reset.

        Caller owns scope: pass only the IDs that should be eligible
        (typically the current playlist's artist set). Empty input returns
        0 without hitting the DB. Reason-code-scoped: only DEFERRED_RETRY
        rows are affected; LOW_CONFIDENCE / AMBIGUOUS_GAP / etc. are left
        untouched.
        """
        ...
