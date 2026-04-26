from dataclasses import replace
from uuid import UUID

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus, ReasonCode
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

    def get_by_id(self, artist_id: UUID) -> BroadcastArtist | None:
        return self._data.get(artist_id)

    def get_by_ids(self, ids: list[UUID]) -> list[BroadcastArtist]:
        return [self._data[i] for i in ids if i in self._data]

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
        self,
        artist_id: UUID,
        status: MatchStatus,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> None:
        current = self._data.get(artist_id)
        if current is None:
            return
        # Mirror Pg UPDATE semantics: unconditionally overwrite reason fields.
        self._data[artist_id] = replace(
            current,
            match_status=status,
            reason_code=reason_code,
            reason_detail=reason_detail,
        )

    def update_embedding(self, artist_id: UUID, embedding: list[float]) -> None:
        if artist := self._data.get(artist_id):
            artist.embedding = embedding
