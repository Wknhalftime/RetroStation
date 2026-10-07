from collections.abc import Collection
from dataclasses import replace
from uuid import UUID

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus, ReasonCode
from backend.repositories.broadcast_artists import BroadcastArtistRepository

# The statuses a re-check rewinds (AUD-R022 D1, D2; spec 2026-10-05 §4.2).
_UNDECIDED = (MatchStatus.NEEDS_REVIEW, MatchStatus.AUTO_REJECTED)


class FakeBroadcastArtistRepository(BroadcastArtistRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, BroadcastArtist] = {}
        # playlist_id -> set of artist_ids (simulates the JOIN through play_events)
        self._playlist_artists: dict[UUID, set[UUID]] = {}

    def register_playlist_artist(self, playlist_id: UUID, artist_id: UUID) -> None:
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

    def get_by_normalized_name(self, normalized_name: str) -> BroadcastArtist | None:
        return next((a for a in self._data.values() if a.normalized_name == normalized_name), None)

    def get_all_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
        ids = self._playlist_artists.get(playlist_id, set())
        return [a for a in self._data.values() if a.id in ids]

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
        ids = self._playlist_artists.get(playlist_id, set())
        return [
            a for a in self._data.values() if a.id in ids and a.match_status == MatchStatus.PENDING
        ]

    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
        ids = self._playlist_artists.get(playlist_id, set())
        return [a for a in self._data.values() if a.id in ids and a.embedding is None]

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

    def update_match_status_if_pending(
        self,
        artist_id: UUID,
        status: MatchStatus,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> bool:
        current = self._data.get(artist_id)
        if current is None or current.match_status != MatchStatus.PENDING:
            return False
        self.update_match_status(artist_id, status, reason_code, reason_detail)
        return True

    def update_embedding(self, artist_id: UUID, embedding: list[float]) -> None:
        if artist := self._data.get(artist_id):
            artist.embedding = embedding

    def reset_deferred_by_ids(self, artist_ids: list[UUID]) -> int:
        if not artist_ids:
            return 0
        ids = set(artist_ids)
        reset = 0
        for artist_id, artist in list(self._data.items()):
            if (
                artist_id in ids
                and artist.match_status == MatchStatus.NEEDS_REVIEW
                and artist.reason_code == ReasonCode.DEFERRED_RETRY
            ):
                self._data[artist_id] = replace(
                    artist,
                    match_status=MatchStatus.PENDING,
                    reason_code=None,
                    reason_detail=None,
                )
                reset += 1
        return reset

    # --- Targeted re-check (spec 2026-10-05 §4.2) ----------------------------------------

    def ids_by_normalized_names(self, names: Collection[str]) -> list[UUID]:
        wanted = set(names)
        return [a.id for a in self._data.values() if a.normalized_name in wanted]

    def rewind_undecided(self, names: Collection[str] | None) -> int:
        # Mirrors Pg: undecided artists in scope go to PENDING with their reason cleared.
        # None means every artist; an empty collection means none.
        wanted = None if names is None else set(names)
        rewound = 0
        for artist_id, artist in list(self._data.items()):
            if artist.match_status not in _UNDECIDED:
                continue
            if wanted is not None and artist.normalized_name not in wanted:
                continue
            self._data[artist_id] = replace(
                artist,
                match_status=MatchStatus.PENDING,
                reason_code=None,
                reason_detail=None,
            )
            rewound += 1
        return rewound

    def playlist_ids_with_pending(self) -> set[UUID]:
        return {
            playlist_id
            for playlist_id, artist_ids in self._playlist_artists.items()
            if any(
                self._data[i].match_status == MatchStatus.PENDING
                for i in artist_ids
                if i in self._data
            )
        }
