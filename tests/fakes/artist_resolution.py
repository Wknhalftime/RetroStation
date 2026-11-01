"""In-memory artist Resolve and Unmatch statements (artist model phase 1a, C75).

Works on the broadcast and match fakes' rows, as ``PgArtistResolutionRepository`` works on
their tables. A playlist "plays" a song when the song is registered on it
(``FakeBroadcastTrackIdentityRepository.register_playlist_identity``), the fakes' stand-in for
``play_events``. ``tests/repositories/test_artist_resolution_contract.py`` runs the same cases
on this fake and on PostgreSQL.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from backend.domain.enums import MatchStatus, ReasonCode
from backend.repositories.artist_resolution import ArtistResolutionRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.matches import FakeMatchRepository

_QUEUE = {MatchStatus.NEEDS_REVIEW, MatchStatus.PENDING}
_PROTECTED = {MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED}
_UNMATCHABLE = {
    MatchStatus.AUTO_MATCHED,
    MatchStatus.MANUAL_MATCHED,
    MatchStatus.AUTO_REJECTED,
    MatchStatus.MANUAL_REJECTED,
}


class FakeArtistResolutionRepository(ArtistResolutionRepository):
    def __init__(
        self,
        *,
        names: FakeBroadcastArtistRepository,
        songs: FakeBroadcastTrackIdentityRepository,
        matches: FakeMatchRepository,
    ) -> None:
        self._names = names._data
        self._songs = songs._data
        self._playlists = songs._playlist_identities
        self._matches = matches._data

    def _song_ids(
        self, broadcast_artist_id: UUID, *, status: MatchStatus | None = None
    ) -> set[UUID]:
        return {
            song.id
            for song in self._songs.values()
            if song.broadcast_artist_id == broadcast_artist_id
            and (status is None or song.match_status == status)
        }

    def exists(self, broadcast_artist_id: UUID) -> bool:
        return broadcast_artist_id in self._names

    def status_of(self, broadcast_artist_id: UUID) -> MatchStatus | None:
        name = self._names.get(broadcast_artist_id)
        return None if name is None else name.match_status

    def set_status(self, broadcast_artist_id: UUID, status: MatchStatus) -> None:
        if (name := self._names.get(broadcast_artist_id)) is not None:
            self._names[broadcast_artist_id] = replace(name, match_status=status)

    def reopen_songs_in_review(self, broadcast_artist_id: UUID) -> None:
        for song_id in self._song_ids(broadcast_artist_id):
            song = self._songs[song_id]
            if song.match_status in _QUEUE:
                self._songs[song_id] = replace(
                    song,
                    match_status=MatchStatus.PENDING,
                    match_tier=None,
                    reason_code=None,
                    reason_detail=None,
                )

    def _delete_matches_of(self, song_ids: set[UUID]) -> None:
        for match_id in [m.id for m in self._matches.values() if m.identity_id in song_ids]:
            del self._matches[match_id]

    def delete_pending_song_matches(self, broadcast_artist_id: UUID) -> None:
        self._delete_matches_of(self._song_ids(broadcast_artist_id, status=MatchStatus.PENDING))

    def playlists_playing(self, broadcast_artist_id: UUID) -> list[UUID]:
        song_ids = self._song_ids(broadcast_artist_id)
        return [playlist for playlist, played in self._playlists.items() if played & song_ids]

    def reject_open_songs(self, broadcast_artist_id: UUID) -> None:
        for song_id in self._song_ids(broadcast_artist_id):
            song = self._songs[song_id]
            if song.match_status not in _PROTECTED:
                self._songs[song_id] = replace(song, match_status=MatchStatus.AUTO_REJECTED)

    def unmatch_if_decided(self, broadcast_artist_id: UUID) -> bool:
        name = self._names.get(broadcast_artist_id)
        if name is None or name.match_status not in _UNMATCHABLE:
            return False
        self._names[broadcast_artist_id] = replace(
            name,
            match_status=MatchStatus.NEEDS_REVIEW,
            reason_code=ReasonCode.USER_UNMATCHED,
            reason_detail=None,
        )
        return True

    def reopen_every_song(self, broadcast_artist_id: UUID) -> None:
        for song_id in self._song_ids(broadcast_artist_id):
            self._songs[song_id] = replace(
                self._songs[song_id],
                match_status=MatchStatus.NEEDS_REVIEW,
                match_tier=None,
                reason_code=ReasonCode.USER_UNMATCHED,
                reason_detail=None,
            )

    def delete_song_matches(self, broadcast_artist_id: UUID) -> None:
        self._delete_matches_of(self._song_ids(broadcast_artist_id))
