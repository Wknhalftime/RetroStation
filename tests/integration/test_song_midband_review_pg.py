"""Acceptance tests: D14, the one-time demotion of the old song mid-band (AUD-R025).

Spec 2026-10-05 §2 D14, an explicit one-time exception to D2: the songs the old song mid-band
auto-matched go back to review once. Migration 0036 does it.
- Which songs: track_identities that are auto_matched and whose best match row scores under 65.
  Under D12 no song auto-matches below 65, so nothing legitimate sits there.
- What it writes: needs_review / LOW_CONFIDENCE with the D14 reason text. The match tier and the
  match row (the suggestion) are kept.
- Nothing else changes: other statuses, songs at 65 and up, songs without a row, artists, rows.
- A second run changes nothing. The rollback script restores only demotions nothing moved since,
  and keeps the schema_migrations row so a restart never re-applies 0036.
- The flow: Re-run Matching's rewind (scope all) returns the demoted songs to pending, and the
  matcher re-scores them under D6, D12 and D13. A demoted song's plays stop resolving to its
  work's master (Lance's Q1 default: they stop playing until approved or re-matched).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    match_identities_for_playlist,
)
from backend.services.matching_reasons import format_low_confidence
from backend.services.matching_recheck_service import rewind_wave
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from tests.fakes.mb_client import FakeMbClient

Conn = psycopg.Connection[Any]

_DB = Path(__file__).resolve().parents[2] / "backend" / "db"
_VERSION = "0036_song_midband_review"
_MIGRATION = _DB / "migrations" / f"{_VERSION}.sql"
_ROLLBACK = _DB / f"rollback_{_VERSION}.sql"

ARTIST = "Van Halen"
NORM = normalize_artist(ARTIST)
MBID = "mbid-van-halen"  # not in the catalog, so Tier 1 uses it as the artist MBID
D14_TEXT = "auto-matched by the old mid-band, back for review (D14)"
UNDECIDED_OR_DECIDED = [
    MatchStatus.PENDING,
    MatchStatus.NEEDS_REVIEW,
    MatchStatus.MANUAL_MATCHED,
    MatchStatus.AUTO_REJECTED,
    MatchStatus.MANUAL_REJECTED,
]


def _connect(url: str) -> Conn:
    return psycopg.connect(url, row_factory=dict_row)


def _d14_detail(percent: int) -> str:
    return f"Score {percent}% — {D14_TEXT}"


# --- Seed helpers --------------------------------------------------------------------------


def _artist(conn: Conn, status: MatchStatus = MatchStatus.AUTO_MATCHED) -> BroadcastArtist:
    repo = PgBroadcastArtistRepository(conn)
    artist = repo.upsert(
        BroadcastArtist(id=uuid4(), original_name=ARTIST, normalized_name=NORM, match_status=status)
    )
    repo.update_match_status(artist.id, status, None, None)
    return artist


def _artist_row(conn: Conn, artist: BroadcastArtist, score: float = 100.0) -> Match:
    """The artist's own match row (Tier 1 reads its target as the artist MBID)."""
    return PgMatchRepository(conn).create(
        Match(
            id=uuid4(),
            artist_id=artist.id,
            target_id=MBID,
            target_type=TargetType.ARTIST,
            confidence_score=score,
            match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
        )
    )


def _file(conn: Conn, title: str = "Some File") -> LibraryFile:
    return PgLibraryFileRepository(conn).upsert(
        LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            audio=AudioMetadata(
                track_title=title,
                normalized_title=normalize_title(title),
                artist_name=ARTIST,
                normalized_artist_name=NORM,
            ),
        )
    )


def _song(
    conn: Conn,
    artist: BroadcastArtist,
    title: str = "Hot For Teacher",
    status: MatchStatus = MatchStatus.AUTO_MATCHED,
    tier: MatchTier = MatchTier.MUSICBRAINZ_ID_EXACT,
) -> BroadcastTrackIdentity:
    """A song as the old matcher left it: an auto-match carries no reason."""
    repo = PgBroadcastTrackIdentityRepository(conn)
    norm = normalize_title(title)
    song = repo.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title=title,
            normalized_title=norm,
            normalized_signature=compute_normalized_signature(NORM, norm),
        )
    )
    if status != MatchStatus.PENDING:
        repo.update_match_status(song.id, status, tier, None, None)
    return song


def _row(
    conn: Conn,
    song: BroadcastTrackIdentity,
    score: float,
    file_id: UUID | None = None,
    tier: MatchTier = MatchTier.MUSICBRAINZ_ID_EXACT,
) -> Match:
    return PgMatchRepository(conn).create(
        Match(
            id=uuid4(),
            identity_id=song.id,
            library_file_id=file_id or _file(conn).id,
            confidence_score=score,
            match_tier=tier,
        )
    )


def _state(conn: Conn, song: BroadcastTrackIdentity) -> dict[str, Any]:
    row = conn.execute(
        """SELECT match_status, match_tier, reason_code, reason_detail
             FROM track_identities WHERE id = %s""",
        (song.id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def _rows(conn: Conn, song: BroadcastTrackIdentity) -> list[tuple[Any, ...]]:
    found = conn.execute(
        """SELECT id, library_file_id, confidence_score, match_tier, work_id
             FROM matches WHERE identity_id = %s ORDER BY confidence_score, id""",
        (song.id,),
    ).fetchall()
    return [tuple(r.values()) for r in found]


def _migrate(conn: Conn) -> int:
    """Run the migration's SQL as the runner does; return how many songs it changed."""
    return conn.execute(_MIGRATION.read_text(encoding="utf-8")).rowcount


def _auto_matched(tier: MatchTier = MatchTier.MUSICBRAINZ_ID_EXACT) -> dict[str, Any]:
    return {
        "match_status": MatchStatus.AUTO_MATCHED.value,
        "match_tier": tier.value,
        "reason_code": None,
        "reason_detail": None,
    }


# --- The migration is registered ------------------------------------------------------------


def test_migration_0036_is_applied(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (_VERSION,)
        ).fetchone()

    assert applied is not None


# --- Which songs, and what it writes --------------------------------------------------------


@pytest.mark.parametrize(
    "tier", [MatchTier.MUSICBRAINZ_ID_EXACT, MatchTier.LOCAL_FILE_FUZZY], ids=["tier1", "tier2"]
)
@pytest.mark.parametrize(
    ("score", "percent"),
    [(55.0, 55), (60.4, 60), (60.5, 61), (64.0, 64), (64.99, 65)],
)
def test_an_auto_match_under_65_goes_back_to_review_with_its_suggestion(
    migrated_db: str, tier: MatchTier, score: float, percent: int
) -> None:
    """The text rounds half up, as format_low_confidence does: 64.99 reads "65%" (cosmetic)."""
    with _connect(migrated_db) as conn:
        song = _song(conn, _artist(conn), tier=tier)
        _row(conn, song, score, tier=tier)
        before = _rows(conn, song)

        assert _migrate(conn) == 1

        assert _state(conn, song) == {
            "match_status": MatchStatus.NEEDS_REVIEW.value,
            "match_tier": tier.value,  # kept
            "reason_code": ReasonCode.LOW_CONFIDENCE.value,
            "reason_detail": _d14_detail(percent),
        }
        assert _rows(conn, song) == before  # the suggestion is kept, untouched


def test_an_auto_match_at_65_stays_matched(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        artist = _artist(conn)
        under = _song(conn, artist, "Panama")
        _row(conn, under, 64.99)
        at = _song(conn, artist, "Jump")
        _row(conn, at, 65.0)

        assert _migrate(conn) == 1

        assert _state(conn, under)["match_status"] == MatchStatus.NEEDS_REVIEW.value
        assert _state(conn, at) == _auto_matched()


@pytest.mark.parametrize("score", [65.0, 80.0, 100.0])
def test_a_strong_auto_match_is_untouched(migrated_db: str, score: float) -> None:
    with _connect(migrated_db) as conn:
        song = _song(conn, _artist(conn))
        _row(conn, song, score)
        before = _rows(conn, song)

        assert _migrate(conn) == 0

        assert _state(conn, song) == _auto_matched()
        assert _rows(conn, song) == before


@pytest.mark.parametrize("status", UNDECIDED_OR_DECIDED)
def test_any_other_status_under_65_is_untouched(migrated_db: str, status: MatchStatus) -> None:
    """D2 still holds for everything but the old mid-band's auto-matches. A score-only predicate
    would demote approvals: the dev DB's 454 manual_matched rows all score 1.0 (manual scale)."""
    with _connect(migrated_db) as conn:
        song = _song(conn, _artist(conn), status=status)
        _row(conn, song, 60.0)
        before = (_state(conn, song), _rows(conn, song))

        assert _migrate(conn) == 0

        assert (_state(conn, song), _rows(conn, song)) == before


def test_an_auto_match_without_a_match_row_is_untouched(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        song = _song(conn, _artist(conn))

        assert _migrate(conn) == 0

        assert _state(conn, song) == _auto_matched()


def test_a_song_is_judged_by_its_best_row(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        artist = _artist(conn)
        strong = _song(conn, artist, "Jump")
        _row(conn, strong, 60.0)
        _row(conn, strong, 90.0)
        weak = _song(conn, artist, "Panama")
        _row(conn, weak, 58.0)
        _row(conn, weak, 62.0)
        weak_rows = _rows(conn, weak)

        assert _migrate(conn) == 1

        assert _state(conn, strong) == _auto_matched()
        assert _state(conn, weak)["reason_detail"] == _d14_detail(62)
        assert _rows(conn, weak) == weak_rows  # both rows kept


@pytest.mark.parametrize(
    "status", [MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED, MatchStatus.NEEDS_REVIEW]
)
def test_artists_are_untouched(migrated_db: str, status: MatchStatus) -> None:
    """Artist matching keeps its own mid-band (D12): an artist at 60 stays as it is."""
    with _connect(migrated_db) as conn:
        artist = _artist(conn, status)
        row = _artist_row(conn, artist, 60.0)

        assert _migrate(conn) == 0

        stored = PgBroadcastArtistRepository(conn).get_by_id(artist.id)
        assert stored is not None and stored.match_status == status
        assert PgMatchRepository(conn).get_by_artist(artist.id) == row


# --- Once only ------------------------------------------------------------------------------


def test_a_second_run_changes_nothing(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        song = _song(conn, _artist(conn))
        _row(conn, song, 60.0)
        assert _migrate(conn) == 1
        after_first = (_state(conn, song), _rows(conn, song))

        assert _migrate(conn) == 0

        assert (_state(conn, song), _rows(conn, song)) == after_first


def test_the_rollback_restores_only_untouched_demotions(migrated_db: str) -> None:
    conn = _connect(migrated_db)
    try:
        songs = PgBroadcastTrackIdentityRepository(conn)
        artist = _artist(conn)
        untouched, rewound, approved, cascaded = (
            _song(conn, artist, title) for title in ("Panama", "Jump", "Unchained", "Dance")
        )
        for song in (untouched, rewound, approved, cascaded):
            _row(conn, song, 60.0)
        matcher_review = _song(conn, artist, "Dreams", MatchStatus.PENDING)
        songs.update_match_status(
            matcher_review.id,
            MatchStatus.NEEDS_REVIEW,
            MatchTier.LOCAL_FILE_FUZZY,
            ReasonCode.LOW_CONFIDENCE,
            format_low_confidence(60.0),
        )
        assert _migrate(conn) == 4
        # A re-check rewinds one (status only, reason cleared).
        conn.execute(
            """UPDATE track_identities
                  SET match_status = 'pending', match_tier = NULL,
                      reason_code = NULL, reason_detail = NULL
                WHERE id = %s""",
            (rewound.id,),
        )
        # Approve writes status and tier only, as routers/matching.py does: the D14 text stays.
        conn.execute(
            """UPDATE track_identities SET match_status = 'manual_matched', match_tier = 'manual'
                WHERE id = %s""",
            (approved.id,),
        )
        # The artist MANUAL_REJECTED cascade flips the status only: the D14 text stays.
        conn.execute(
            "UPDATE track_identities SET match_status = 'auto_rejected' WHERE id = %s",
            (cascaded.id,),
        )

        conn.execute(_ROLLBACK.read_text(encoding="utf-8"))

        assert _state(conn, untouched) == _auto_matched()
        assert _state(conn, rewound)["match_status"] == MatchStatus.PENDING.value
        assert _state(conn, approved)["match_status"] == MatchStatus.MANUAL_MATCHED.value
        assert _state(conn, cascaded)["match_status"] == MatchStatus.AUTO_REJECTED.value
        assert _state(conn, matcher_review)["match_status"] == MatchStatus.NEEDS_REVIEW.value
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (_VERSION,)
        ).fetchone()
        # Kept: 0036 stays in migrations/, so the row stops the next start from re-applying it.
        assert applied is not None
    finally:
        conn.rollback()  # nothing this test wrote is left in the shared test database
        conn.close()


# --- The flow: Re-run Matching after the migration ------------------------------------------


def _playlist(conn: Conn) -> BroadcastPlaylist:
    station = PgBroadcastStationRepository(conn).create(
        BroadcastStation(id=uuid4(), call_letters="KVHN")
    )
    return PgBroadcastPlaylistRepository(conn).create(
        BroadcastPlaylist(
            id=uuid4(), name=f"{uuid4().hex}.csv", content_hash=uuid4().hex, station_id=station.id
        )
    )


def _play(conn: Conn, playlist: BroadcastPlaylist, song: BroadcastTrackIdentity) -> UUID:
    event = PgBroadcastPlayEventRepository(conn).create(
        BroadcastPlayEvent(
            id=uuid4(), identity_id=song.id, playlist_id=playlist.id, played_at=datetime.now(UTC)
        )
    )
    return event.id


def _match_playlist(conn: Conn, playlist: BroadcastPlaylist) -> None:
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


def test_rerun_matching_rescores_the_demoted_songs_under_the_new_rules(migrated_db: str) -> None:
    """The intended flow: migration, then Re-run Matching (rewind scope all, then the matcher).

    Untagged files under an auto-matched artist, so Tier 1 reaches Step C (D13). Real title
    scoring: "Missing" vs "Missing Persons" scores 63.6; the exact title scores 100.
    """
    with _connect(migrated_db) as conn:
        playlist = _playlist(conn)
        artist = _artist(conn)
        _artist_row(conn, artist)
        persons = _file(conn, "Missing Persons")
        teacher = _file(conn, "Hot for Teacher")
        _file(conn, "Totally Unrelated Words Here")
        review = _song(conn, artist, "Missing")
        rematched = _song(conn, artist, "Hot For Teacher")
        floored = _song(conn, artist, "Enter Sandman")
        for song, file_id in ((review, persons.id), (rematched, teacher.id), (floored, None)):
            _row(conn, song, 60.0, file_id)
            _play(conn, playlist, song)
        assert _migrate(conn) == 3

        counts = rewind_wave(
            None, PgBroadcastArtistRepository(conn), PgBroadcastTrackIdentityRepository(conn)
        )
        _match_playlist(conn, playlist)

        assert counts.songs == 3
        assert _state(conn, review) == {
            "match_status": MatchStatus.NEEDS_REVIEW.value,
            "match_tier": MatchTier.LOCAL_FILE_FUZZY.value,
            "reason_code": ReasonCode.LOW_CONFIDENCE.value,
            "reason_detail": format_low_confidence(63.63636363636363),
        }
        assert [r[1] for r in _rows(conn, review)] == [persons.id]
        assert _state(conn, rematched)["match_status"] == MatchStatus.AUTO_MATCHED.value
        assert [(r[1], r[2]) for r in _rows(conn, rematched)] == [(teacher.id, 100.0)]
        assert _state(conn, floored)["match_status"] == MatchStatus.AUTO_REJECTED.value
        assert _rows(conn, floored) == []


def test_a_demoted_songs_plays_stop_resolving_to_its_works_master(migrated_db: str) -> None:
    """Playout, M3U export and cue analysis read play_file_resolution, which routes only
    auto_matched and manual_matched songs to their work's master (file_id): a demoted song's
    plays play nothing until it is approved or re-matched (Q1 default: they stop playing)."""
    with _connect(migrated_db) as conn:
        playlist = _playlist(conn)
        song = _song(conn, _artist(conn))
        kept = _file(conn, "Hot For Teacher (Live)")
        master = _file(conn, "Hot For Teacher")
        conn.execute(
            "INSERT INTO artists (id, name, sort_name) VALUES ('d14-artist', %s, %s)",
            (ARTIST, ARTIST),
        )
        conn.execute(
            "INSERT INTO works (id, title, artist_id) VALUES ('d14-work', %s, 'd14-artist')",
            ("Hot For Teacher",),
        )
        conn.execute(
            "UPDATE library_files SET work_id = 'd14-work' WHERE id = ANY(%s)",
            ([kept.id, master.id],),
        )
        conn.execute(
            "INSERT INTO song_masters (work_id, preferred_file_id) VALUES ('d14-work', %s)",
            (master.id,),
        )
        _row(conn, song, 60.0, kept.id)
        play = _play(conn, playlist, song)

        def resolved() -> tuple[UUID | None, UUID | None]:
            found = conn.execute(
                """SELECT matched_file_id, file_id FROM play_file_resolution
                    WHERE play_event_id = %s""",
                (play,),
            ).fetchone()
            assert found is not None
            return found["matched_file_id"], found["file_id"]

        assert resolved() == (kept.id, master.id)
        _migrate(conn)

        assert resolved() == (None, None)
