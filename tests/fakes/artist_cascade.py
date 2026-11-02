"""In-memory whole-name cascade reads and writes (artist model phase 1b, S1, S2, S3, S5).

Works on the broadcast, match, library file and play event fakes' rows, as
``PgArtistCascadeRepository`` works on their tables. A line's match is the one
``play_file_resolution`` plays: a match to a file, the highest score first, then the oldest,
then the id. ``tests/repositories/test_artist_cascade_contract.py`` runs the same cases on this
fake and on PostgreSQL. ``lock`` takes no lock here: a fake has no other transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

from backend.domain.artist_cascade import CascadeLine
from backend.domain.enums import MatchStatus, ReasonCode
from backend.domain.matching import Match
from backend.repositories.artist_cascade import ArtistCascadeRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_play_events import FakeBroadcastPlayEventRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository


class FakeArtistCascadeRepository(ArtistCascadeRepository):
    def __init__(
        self,
        *,
        names: FakeBroadcastArtistRepository,
        songs: FakeBroadcastTrackIdentityRepository,
        matches: FakeMatchRepository,
        files: FakeLibraryFileRepository,
        plays: FakeBroadcastPlayEventRepository,
    ) -> None:
        self._names = names._data
        self._songs = songs._data
        self._matches = matches._data
        self._files = files._data
        self._plays = plays._data

    def _playing_match(self, identity_id: UUID) -> Match | None:
        to_a_file = [
            m
            for m in self._matches.values()
            if m.identity_id == identity_id and m.library_file_id is not None
        ]
        to_a_file.sort(key=lambda m: (-m.confidence_score, m.created_at, str(m.id)))
        return to_a_file[0] if to_a_file else None

    def _same_name(self, identity_id: UUID, normalized_name: str) -> bool:
        match = self._playing_match(identity_id)
        if match is None or match.library_file_id is None:
            return False
        file = self._files.get(match.library_file_id)
        return file is not None and file.audio.normalized_artist_name == normalized_name

    def lines_of(self, broadcast_artist_id: UUID, *, lock: bool = False) -> list[CascadeLine]:
        name = self._names.get(broadcast_artist_id)
        if name is None:
            return []
        return [
            CascadeLine(
                identity_id=song.id,
                status=song.match_status,
                same_name=self._same_name(song.id, name.normalized_name),
                plays=sum(1 for play in self._plays.values() if play.identity_id == song.id),
            )
            for song in self._songs.values()
            if song.broadcast_artist_id == broadcast_artist_id
        ]

    def reject_lines(self, identity_ids: Sequence[UUID]) -> None:
        for identity_id in identity_ids:
            if (song := self._songs.get(identity_id)) is not None:
                self._songs[identity_id] = replace(song, match_status=MatchStatus.AUTO_REJECTED)

    def reopen_lines(self, identity_ids: Sequence[UUID]) -> None:
        for identity_id in identity_ids:
            if (song := self._songs.get(identity_id)) is not None:
                self._songs[identity_id] = replace(
                    song,
                    match_status=MatchStatus.NEEDS_REVIEW,
                    match_tier=None,
                    reason_code=ReasonCode.USER_UNMATCHED,
                    reason_detail=None,
                )

    def delete_matches_of_lines(self, identity_ids: Sequence[UUID]) -> None:
        wanted = set(identity_ids)
        for match_id in [m.id for m in self._matches.values() if m.identity_id in wanted]:
            del self._matches[match_id]
