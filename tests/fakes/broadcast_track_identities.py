from uuid import UUID

from backend.domain.broadcast import BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.services.matching_reasons import ReasonCode


class FakeBroadcastTrackIdentityRepository(BroadcastTrackIdentityRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastTrackIdentity] = {}
        self._playlist_identities: dict[UUID, set[UUID]] = {}
        self._reason_codes: dict[UUID, ReasonCode | None] = {}
        self._reason_details: dict[UUID, str | None] = {}

    def register_playlist_identity(
        self, playlist_id: UUID, identity_id: UUID
    ) -> None:
        self._playlist_identities.setdefault(playlist_id, set()).add(
            identity_id
        )

    def upsert(self, identity: BroadcastTrackIdentity) -> BroadcastTrackIdentity:
        existing = self.get_by_signature(identity.normalized_signature)
        if existing:
            return existing
        self._data[identity.id] = identity
        return identity

    def get_by_id(self, identity_id: UUID) -> BroadcastTrackIdentity | None:
        return self._data.get(identity_id)

    def get_by_signature(
        self, normalized_signature: str
    ) -> BroadcastTrackIdentity | None:
        return next(
            (i for i in self._data.values()
             if i.normalized_signature == normalized_signature), None
        )

    def get_for_artist(
        self, broadcast_artist_id: UUID
    ) -> list[BroadcastTrackIdentity]:
        return [
            i for i in self._data.values()
            if i.broadcast_artist_id == broadcast_artist_id
        ]

    def get_pending_for_playlist(
        self, playlist_id: UUID
    ) -> list[BroadcastTrackIdentity]:
        ids = self._playlist_identities.get(playlist_id, set())
        return [
            i for i in self._data.values()
            if i.id in ids and i.match_status == MatchStatus.PENDING
        ]

    def get_unembedded_for_playlist(
        self, playlist_id: UUID
    ) -> list[BroadcastTrackIdentity]:
        ids = self._playlist_identities.get(playlist_id, set())
        return [
            i for i in self._data.values()
            if i.id in ids and i.embedding is None
        ]

    def update_match_status(
        self,
        identity_id: UUID,
        status: MatchStatus,
        tier: MatchTier | None,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> None:
        identity = self._data.get(identity_id)
        if identity is None:
            return
        identity.match_status = status
        identity.match_tier = tier
        # Unconditionally overwrite — mirrors Pg UPDATE semantics.
        self._reason_codes[identity_id] = reason_code
        self._reason_details[identity_id] = reason_detail

    def update_embedding(self, identity_id: UUID, embedding: list[float]) -> None:
        if identity := self._data.get(identity_id):
            identity.embedding = embedding

    def bulk_reject_by_artist(self, broadcast_artist_id: UUID) -> None:
        for identity in self._data.values():
            if identity.broadcast_artist_id == broadcast_artist_id:
                identity.match_status = MatchStatus.AUTO_REJECTED
                identity.match_tier = MatchTier.UNCLASSIFIED

