from uuid import UUID

from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LogIdentity
from backend.repositories.log_identities import LogIdentityRepository


class FakeLogIdentityRepository(LogIdentityRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LogIdentity] = {}
        self._playlist_identities: dict[UUID, set[UUID]] = {}

    def register_playlist_identity(self, playlist_id: UUID, identity_id: UUID) -> None:
        self._playlist_identities.setdefault(playlist_id, set()).add(identity_id)

    def upsert(self, identity: LogIdentity) -> LogIdentity:
        existing = self.get_by_signature(identity.normalized_signature)
        if existing:
            return existing
        self._data[identity.id] = identity
        return identity

    def get_by_id(self, id: UUID) -> LogIdentity | None:
        return self._data.get(id)

    def get_by_signature(self, normalized_signature: str) -> LogIdentity | None:
        return next(
            (i for i in self._data.values()
             if i.normalized_signature == normalized_signature), None
        )

    def get_for_artist(self, artist_id: UUID) -> list[LogIdentity]:
        return [i for i in self._data.values() if i.artist_id == artist_id]

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        ids = self._playlist_identities.get(playlist_id, set())
        return [
            i for i in self._data.values()
            if i.id in ids and i.match_status == MatchStatus.PENDING
        ]

    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        ids = self._playlist_identities.get(playlist_id, set())
        return [i for i in self._data.values() if i.id in ids and i.embedding is None]

    def update_match_status(self, id: UUID, status: MatchStatus, tier: MatchTier) -> None:
        if identity := self._data.get(id):
            identity.match_status = status
            identity.match_tier = tier

    def update_embedding(self, id: UUID, embedding: list[float]) -> None:
        if identity := self._data.get(id):
            identity.embedding = embedding

    def bulk_reject_by_artist(self, artist_id: UUID) -> None:
        for identity in self._data.values():
            if identity.artist_id == artist_id:
                identity.match_status = MatchStatus.AUTO_REJECTED
                identity.match_tier = MatchTier.UNKNOWN
