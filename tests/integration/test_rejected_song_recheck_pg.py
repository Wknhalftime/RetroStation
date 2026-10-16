"""A parked song is re-checked on the next run without its rejected file (spec 2026-10-13
R1-R3), against PostgreSQL.

- Another file can match it (a strong one may auto-match, R3); its match row never points at
  the rejected file.
- With nothing left, the song ends auto_rejected with no match rows.
"""

from __future__ import annotations

from uuid import UUID

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.domain.broadcast import BroadcastPlaylist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    match_identities_for_playlist,
)
from tests.fakes.mb_client import FakeMbClient
from tests.integration.test_matching_recheck_pg import (
    Conn,
    _artist,
    _connect,
    _file,
    _match_rows,
    _play,
    _playlist,
    _song,
    _song_row,
    _station,
)


def _park(conn: Conn, song: BroadcastTrackIdentity, rejected: UUID) -> None:
    """The state record_song_rejection writes (spec 2026-10-13 §1)."""
    conn.execute(
        """UPDATE track_identities
              SET match_status = 'manual_rejected', match_tier = NULL,
                  reason_code = 'USER_UNMATCHED', reason_detail = NULL,
                  rejected_file_ids = %s
            WHERE id = %s""",
        ([rejected], song.id),
    )


def _rewind_and_match(conn: Conn, playlist: BroadcastPlaylist) -> None:
    assert PgBroadcastTrackIdentityRepository(conn).rewind_undecided(None) == 1
    match_identities_for_playlist(
        playlist_id=playlist.id,
        repos=IdentityMatchingRepos(
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=PgLibraryFileRepository(conn),
            rules_repo=PgMappingRuleRepository(conn),
            catalog_repo=PgArtistRepository(conn),
        ),
        mb_client=FakeMbClient(),
    )


def test_a_rewound_parked_song_matches_another_file_not_the_rejected_one(
    migrated_db: str,
) -> None:
    with _connect(migrated_db) as conn:
        playlist = _playlist(conn, _station(conn))
        artist = _artist(conn, "ABBA", MatchStatus.PENDING)
        song = _song(conn, artist, "Waterloo", MatchStatus.NEEDS_REVIEW)
        rejected = _file(conn, title="Waterloo")
        other = _file(conn, title="Waterloo")
        _park(conn, song, rejected.id)
        _play(conn, playlist, song)

        _rewind_and_match(conn, playlist)

        rows = conn.execute(
            "SELECT library_file_id FROM matches WHERE identity_id = %s", (song.id,)
        ).fetchall()
        assert [r["library_file_id"] for r in rows] == [other.id]
        assert _song_row(conn, song)["match_status"] in {
            MatchStatus.AUTO_MATCHED.value,
            MatchStatus.NEEDS_REVIEW.value,
        }
        assert list(_song_row(conn, song)["rejected_file_ids"]) == [rejected.id]


def test_a_rewound_parked_song_with_nothing_left_is_auto_rejected(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        playlist = _playlist(conn, _station(conn))
        artist = _artist(conn, "ABBA", MatchStatus.PENDING)
        song = _song(conn, artist, "Waterloo", MatchStatus.NEEDS_REVIEW)
        rejected = _file(conn, title="Waterloo")
        _park(conn, song, rejected.id)
        _play(conn, playlist, song)

        _rewind_and_match(conn, playlist)

        assert _song_row(conn, song)["match_status"] == MatchStatus.AUTO_REJECTED.value
        assert _match_rows(conn, song) == 0
