from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.broadcast import BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode


class BroadcastTrackIdentityRepository(ABC):
    @abstractmethod
    def upsert(self, identity: BroadcastTrackIdentity) -> BroadcastTrackIdentity:
        """Insert or ignore on normalized_signature conflict. Always returns stored row."""
        ...

    @abstractmethod
    def get_by_id(self, identity_id: UUID) -> BroadcastTrackIdentity | None: ...

    @abstractmethod
    def get_by_signature(self, normalized_signature: str) -> BroadcastTrackIdentity | None: ...

    @abstractmethod
    def get_for_artist(self, artist_id: UUID) -> list[BroadcastTrackIdentity]: ...

    @abstractmethod
    def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastTrackIdentity]:
        """Identities linked to this playlist's events with match_status=PENDING
        and their log_artist already resolved."""
        ...

    @abstractmethod
    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[BroadcastTrackIdentity]:
        """Identities linked to this playlist's events with embedding IS NULL."""
        ...

    @abstractmethod
    def update_match_status(
        self,
        identity_id: UUID,
        status: MatchStatus,
        tier: MatchTier | None,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> None:
        """Update match status plus optional reason metadata.

        tier is required to be passed explicitly — even as None — so callers
        make a conscious choice for statuses like AUTO_MATCHED that require a
        tier and cannot silently null it by omitting the argument. reason_code
        / reason_detail default to None; passing None explicitly clears any
        previously-persisted reason.
        """
        ...

    @abstractmethod
    def update_embedding(self, identity_id: UUID, embedding: list[float]) -> None: ...

    @abstractmethod
    def bulk_reject_by_artist(self, artist_id: UUID) -> None:
        """Set all identities for this artist to AUTO_REJECTED."""
        ...

    @abstractmethod
    def bulk_defer_by_artist(self, broadcast_artist_id: UUID) -> int:
        """Set PENDING identities under this artist to NEEDS_REVIEW/DEFERRED_RETRY.

        Mirrors ``bulk_reject_by_artist`` but uses the retryable DEFERRED_RETRY
        reason code (so the next playlist's reset path picks them back up)
        instead of the terminal AUTO_REJECTED status. Already-matched identities
        are left alone. Returns rows changed.
        """
        ...

    @abstractmethod
    def reset_deferred_by_artist_ids(self, artist_ids: list[UUID]) -> int:
        """Reset NEEDS_REVIEW/DEFERRED_RETRY identities under the given artists
        back to PENDING. Returns rows reset.

        Empty input returns 0 without hitting the DB. Reason-code-scoped:
        AUTO_REJECTED rows and non-DEFERRED_RETRY NEEDS_REVIEW rows are
        untouched.
        """
        ...
