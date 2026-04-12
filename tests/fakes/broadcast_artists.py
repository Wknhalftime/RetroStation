from uuid import UUID

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus, MatchTier
from backend.repositories.broadcast_artists import BroadcastArtistRepository


class FakeBroadcastArtistRepository(BroadcastArtistRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastArtist] = {}
        # playlist_id -> set of artist_ids (simulates the JOIN through play_events)
        self._playlist_artists: dict[UUID, set[UUID]] = {}

    def register_playlist_artist(
        self, playlist_id: UUID, artist_id: UUID
    ) -> None:
        """Test helper: record that an artist appears in a playlist."""
        self._playlist_artists.setdefault(playlist_id, set()).add(artist_id)

    def upsert(self, artist: BroadcastArtist) -> BroadcastArtist:
        existing = self.get_by_normalized_name(artist.normalized_name)
        if existing:
            return existing
        self._data[artist.id] = artist
        return artist

    def get_by_id(self, id: UUID) -> BroadcastArtist | None:
        return self._data.get(id)

    def get_by_normalized_name(
        self, normalized_name: str
    ) -> BroadcastArtist | None:
        return next(
            (a for a in self._data.values()
             if a.normalized_name == normalized_name), None
        )

    def get_all_for_playlist(
        self, playlist_id: UUID
    ) -> list[BroadcastArtist]:
        ids = self._playlist_artists.get(playlist_id, set())
        return [a for a in self._data.values() if a.id in ids]

    def get_pending_for_playlist(
        self, playlist_id: UUID
    ) -> list[BroadcastArtist]:
        ids = self._playlist_artists.get(playlist_id, set())
        return [
            a for a in self._data.values()
            if a.id in ids and a.match_status == MatchStatus.PENDING
        ]

    def get_unembedded_for_playlist(
        self, playlist_id: UUID
    ) -> list[BroadcastArtist]:
        ids = self._playlist_artists.get(playlist_id, set())
        return [
            a for a in self._data.values()
            if a.id in ids and a.embedding is None
        ]

    def update_match_status(
        self, id: UUID, status: MatchStatus, tier: MatchTier | None = None
    ) -> None:
        if artist := self._data.get(id):
            artist.match_status = status
            if tier is not None:
                artist.match_tier = tier

    def update_embedding(self, id: UUID, embedding: list[float]) -> None:
        if artist := self._data.get(id):
            artist.embedding = embedding
