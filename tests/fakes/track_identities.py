from uuid import UUID

from backend.domain.broadcast import BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier
from backend.repositories.track_identities import BroadcastTrackIdentityRepository


class FakeBroadcastTrackIdentityRepository(BroadcastTrackIdentityRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastTrackIdentity] = {}
        self._playlist_identities: dict[UUID, set[UUID]] = {}

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

    def get_by_id(self, id: UUID) -> BroadcastTrackIdentity | None:
        return self._data.get(id)

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
        self, id: UUID, status: MatchStatus, tier: MatchTier
    ) -> None:
        if identity := self._data.get(id):
            identity.match_status = status
            identity.match_tier = tier

    def update_embedding(self, id: UUID, embedding: list[float]) -> None:
        if identity := self._data.get(id):
            identity.embedding = embedding

    def bulk_reject_by_artist(self, broadcast_artist_id: UUID) -> None:
        for identity in self._data.values():
            if identity.broadcast_artist_id == broadcast_artist_id:
                identity.match_status = MatchStatus.AUTO_REJECTED
                identity.match_tier = MatchTier.UNCLASSIFIED
