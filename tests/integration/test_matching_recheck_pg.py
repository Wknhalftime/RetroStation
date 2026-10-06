"""The targeted re-check against PostgreSQL (spec 2026-10-05 §4.2; AUD-R022 D1, D2; D11).

- D11: an upsert keeps library_files.indexed_at when size and mtime are unchanged and moves it
  when either changed, so a full scan's wave holds only truly changed files.
- The wave: the distinct, non-empty artist names of files indexed or gone missing strictly
  after a time. The watermark: the started_at of the newest COMPLETED row of one task type.
- The rewinds: needs_review and auto_rejected go to pending, with reasons (and the song tier)
  cleared. Match rows and rejections are kept. None means everything; an empty collection
  means nothing. Pending and decided rows are never touched.
- The fan-out sets. The artist cascades delete the flipped songs' match rows.
- The replacement lets a rewound song re-match the very file it kept:
  UNIQUE(identity_id, library_file_id), with no ON CONFLICT.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import (
    EnrichmentStatus,
    MatchStatus,
    MatchTier,
    ReasonCode,
    TaskStatus,
    TaskType,
)
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.domain.system import TaskProgress
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    match_identities_for_playlist,
)
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from tests.fakes.mb_client import FakeMbClient

Conn = psycopg.Connection[Any]

W = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)
ABBA = normalize_artist("ABBA")
UNDECIDED = [MatchStatus.NEEDS_REVIEW, MatchStatus.AUTO_REJECTED]
NOT_UNDECIDED = [
    MatchStatus.PENDING,
    MatchStatus.AUTO_MATCHED,
    MatchStatus.MANUAL_MATCHED,
    MatchStatus.MANUAL_REJECTED,
]


def _connect(url: str) -> Conn:
    return psycopg.connect(url, row_factory=dict_row)


# --- Seed helpers --------------------------------------------------------------------------


def _file(
    conn: Conn,
    *,
    artist: str | None = ABBA,
    title: str = "Waterloo",
    path: str | None = None,
    size: int | None = 100,
    mtime: int | None = 1,
) -> LibraryFile:
    return PgLibraryFileRepository(conn).upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path or f"/music/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
            file_size=size,
            file_mtime_ns=mtime,
            audio=AudioMetadata(
                track_title=title,
                normalized_title=normalize_title(title),
                artist_name=artist,
                normalized_artist_name=artist,
            ),
        )
    )


def _changed(
    conn: Conn, name: str | None, *, indexed_at: datetime, missing_since: datetime | None = None
) -> None:
    """A file of artist ``name`` with explicit timestamps (an upsert stamps NOW())."""
    f = _file(conn, artist=name)
    conn.execute(
        """UPDATE library_files
              SET indexed_at = %s,
                  missing_since = %s,
                  file_status = CASE WHEN %s::timestamptz IS NULL THEN 'present'
                                     ELSE 'missing' END
            WHERE id = %s""",
        (indexed_at, missing_since, missing_since, f.id),
    )


def _indexed_at(conn: Conn, file_id: UUID) -> datetime:
    row = conn.execute("SELECT indexed_at FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None
    stamp: datetime = row["indexed_at"]
    return stamp


def _backdate(conn: Conn, file_id: UUID) -> None:
    conn.execute("UPDATE library_files SET indexed_at = %s WHERE id = %s", (LONG_AGO, file_id))


def _progress(conn: Conn, task_type: TaskType, status: TaskStatus, started_at: datetime) -> None:
    PgTaskProgressRepository(conn).upsert(
        TaskProgress(
            task_id=uuid4().hex,
            task_type=task_type,
            status=status,
            progress_data={},
            started_at=started_at,
            updated_at=started_at,
            completed_at=None if status == TaskStatus.RUNNING else started_at,
        )
    )


def _artist(conn: Conn, name: str, status: MatchStatus) -> BroadcastArtist:
    repo = PgBroadcastArtistRepository(conn)
    artist = repo.upsert(
        BroadcastArtist(
            id=uuid4(),
            original_name=name,
            normalized_name=normalize_artist(name),
            match_status=status,
        )
    )
    if status != MatchStatus.PENDING:
        repo.update_match_status(artist.id, status, ReasonCode.LOW_CONFIDENCE, "stale reason")
    return artist


def _song(
    conn: Conn, artist: BroadcastArtist, title: str, status: MatchStatus
) -> BroadcastTrackIdentity:
    repo = PgBroadcastTrackIdentityRepository(conn)
    norm = normalize_title(title)
    song = repo.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title=title,
            normalized_title=norm,
            normalized_signature=compute_normalized_signature(artist.normalized_name, norm),
            match_status=status,
        )
    )
    if status != MatchStatus.PENDING:
        repo.update_match_status(
            song.id, status, MatchTier.LOCAL_FILE_FUZZY, ReasonCode.LOW_CONFIDENCE, "stale"
        )
    return song


def _suggest(conn: Conn, song: BroadcastTrackIdentity, file_id: UUID | None = None) -> None:
    PgMatchRepository(conn).create(
        Match(
            id=uuid4(),
            identity_id=song.id,
            library_file_id=file_id,
            confidence_score=70.0,
            match_tier=MatchTier.LOCAL_FILE_FUZZY,
        )
    )


def _match_rows(conn: Conn, song: BroadcastTrackIdentity) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM matches WHERE identity_id = %s", (song.id,)
    ).fetchone()
    assert row is not None
    return int(row["n"])


def _station(conn: Conn) -> BroadcastStation:
    return PgBroadcastStationRepository(conn).create(
        BroadcastStation(id=uuid4(), call_letters="KRCK")
    )


def _playlist(conn: Conn, station: BroadcastStation) -> BroadcastPlaylist:
    return PgBroadcastPlaylistRepository(conn).create(
        BroadcastPlaylist(
            id=uuid4(),
            name=f"{uuid4().hex}.csv",
            content_hash=uuid4().hex,
            station_id=station.id,
        )
    )


def _play(conn: Conn, playlist: BroadcastPlaylist, song: BroadcastTrackIdentity) -> None:
    PgBroadcastPlayEventRepository(conn).create(
        BroadcastPlayEvent(
            id=uuid4(),
            identity_id=song.id,
            playlist_id=playlist.id,
            played_at=datetime.now(UTC),
        )
    )


def _artist_row(conn: Conn, artist: BroadcastArtist) -> dict[str, Any]:
    row = conn.execute(
        "SELECT match_status, reason_code, reason_detail FROM broadcast_artists WHERE id = %s",
        (artist.id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def _song_row(conn: Conn, song: BroadcastTrackIdentity) -> dict[str, Any]:
    row = conn.execute(
        """SELECT match_status, match_tier, reason_code, reason_detail, rejected_file_ids
             FROM track_identities WHERE id = %s""",
        (song.id,),
    ).fetchone()
    assert row is not None
    return dict(row)


# --- D11: the scanner's indexed_at ---------------------------------------------------------


def test_an_unchanged_rescan_keeps_indexed_at(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        path = f"/music/{uuid4().hex}.flac"
        first = _file(conn, path=path)
        _backdate(conn, first.id)

        _file(conn, path=path)  # a full scan walking the same, unchanged file

        assert _indexed_at(conn, first.id) == LONG_AGO


@pytest.mark.parametrize(("size", "mtime"), [(101, 1), (100, 2)])
def test_a_changed_size_or_mtime_moves_indexed_at(migrated_db: str, size: int, mtime: int) -> None:
    with _connect(migrated_db) as conn:
        path = f"/music/{uuid4().hex}.flac"
        first = _file(conn, path=path)
        _backdate(conn, first.id)

        _file(conn, path=path, size=size, mtime=mtime)

        assert _indexed_at(conn, first.id) > LONG_AGO


# --- The wave and the watermark ------------------------------------------------------------


def test_names_changed_since_are_files_indexed_or_gone_missing_after(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        later = W + timedelta(minutes=1)
        _changed(conn, "abba", indexed_at=later)
        _changed(conn, "abba", indexed_at=W + timedelta(hours=1))
        _changed(conn, "queen", indexed_at=LONG_AGO, missing_since=later)
        _changed(conn, "blondie", indexed_at=W - timedelta(minutes=1))
        _changed(conn, "kiss", indexed_at=W)  # exactly at the watermark: not after it
        _changed(conn, None, indexed_at=later)
        _changed(conn, "", indexed_at=later)

        names = PgLibraryFileRepository(conn).normalized_artist_names_changed_since(W)

        assert names == {"abba", "queen"}


def test_the_watermark_is_the_start_of_the_newest_completed_run(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        _progress(conn, TaskType.MATCHING_RECHECK, TaskStatus.COMPLETED, W - timedelta(hours=2))
        _progress(conn, TaskType.MATCHING_RECHECK, TaskStatus.COMPLETED, W)
        _progress(conn, TaskType.MATCHING_RECHECK, TaskStatus.FAILED, W + timedelta(hours=1))
        _progress(conn, TaskType.MATCHING_RECHECK, TaskStatus.RUNNING, W + timedelta(hours=2))
        _progress(conn, TaskType.MB_ENRICHMENT, TaskStatus.COMPLETED, W + timedelta(hours=3))

        repo = PgTaskProgressRepository(conn)

        assert repo.last_completed_started_at(TaskType.MATCHING_RECHECK) == W


def test_no_completed_run_means_no_watermark(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        _progress(conn, TaskType.MATCHING_RECHECK, TaskStatus.FAILED, W)

        repo = PgTaskProgressRepository(conn)

        assert repo.last_completed_started_at(TaskType.MATCHING_RECHECK) is None


# --- The artist rewind ---------------------------------------------------------------------


@pytest.mark.parametrize("status", UNDECIDED)
def test_artist_rewind_returns_an_undecided_artist_to_pending(
    migrated_db: str, status: MatchStatus
) -> None:
    with _connect(migrated_db) as conn:
        artist = _artist(conn, "ABBA", status)

        assert PgBroadcastArtistRepository(conn).rewind_undecided(None) == 1

        assert _artist_row(conn, artist) == {
            "match_status": MatchStatus.PENDING.value,
            "reason_code": None,
            "reason_detail": None,
        }


@pytest.mark.parametrize("status", NOT_UNDECIDED)
def test_artist_rewind_never_touches_a_pending_or_decided_artist(
    migrated_db: str, status: MatchStatus
) -> None:
    with _connect(migrated_db) as conn:
        artist = _artist(conn, "ABBA", status)

        assert PgBroadcastArtistRepository(conn).rewind_undecided(None) == 0

        assert _artist_row(conn, artist)["match_status"] == status.value


def test_artist_rewind_is_scoped_by_normalized_name(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        abba = _artist(conn, "ABBA", MatchStatus.NEEDS_REVIEW)
        queen = _artist(conn, "Queen", MatchStatus.NEEDS_REVIEW)

        assert PgBroadcastArtistRepository(conn).rewind_undecided([ABBA]) == 1

        assert _artist_row(conn, abba)["match_status"] == MatchStatus.PENDING.value
        assert _artist_row(conn, queen)["match_status"] == MatchStatus.NEEDS_REVIEW.value


def test_artist_rewind_of_no_names_rewinds_nothing(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        abba = _artist(conn, "ABBA", MatchStatus.NEEDS_REVIEW)

        assert PgBroadcastArtistRepository(conn).rewind_undecided([]) == 0

        assert _artist_row(conn, abba)["match_status"] == MatchStatus.NEEDS_REVIEW.value


def test_ids_by_normalized_names(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        abba = _artist(conn, "ABBA", MatchStatus.AUTO_MATCHED)
        _artist(conn, "Queen", MatchStatus.NEEDS_REVIEW)
        repo = PgBroadcastArtistRepository(conn)

        assert repo.ids_by_normalized_names([ABBA, "nobody"]) == [abba.id]
        assert repo.ids_by_normalized_names([]) == []


# --- The song rewind -----------------------------------------------------------------------


@pytest.mark.parametrize("status", UNDECIDED)
def test_song_rewind_is_status_only(migrated_db: str, status: MatchStatus) -> None:
    with _connect(migrated_db) as conn:
        song = _song(conn, _artist(conn, "ABBA", MatchStatus.AUTO_MATCHED), "Waterloo", status)
        _suggest(conn, song)
        rejected = uuid4()
        conn.execute(
            "UPDATE track_identities SET rejected_file_ids = %s WHERE id = %s",
            ([rejected], song.id),
        )

        assert PgBroadcastTrackIdentityRepository(conn).rewind_undecided(None) == 1

        row = _song_row(conn, song)
        assert row["match_status"] == MatchStatus.PENDING.value
        assert (row["match_tier"], row["reason_code"], row["reason_detail"]) == (None, None, None)
        assert list(row["rejected_file_ids"]) == [rejected]
        assert _match_rows(conn, song) == 1  # the old suggestion stays until it is replaced


@pytest.mark.parametrize("status", NOT_UNDECIDED)
def test_song_rewind_never_touches_a_pending_or_decided_song(
    migrated_db: str, status: MatchStatus
) -> None:
    with _connect(migrated_db) as conn:
        song = _song(conn, _artist(conn, "ABBA", MatchStatus.AUTO_MATCHED), "Waterloo", status)
        _suggest(conn, song)

        assert PgBroadcastTrackIdentityRepository(conn).rewind_undecided(None) == 0

        assert _song_row(conn, song)["match_status"] == status.value
        assert _match_rows(conn, song) == 1


def test_song_rewind_is_scoped_by_broadcast_artist(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        abba = _artist(conn, "ABBA", MatchStatus.AUTO_MATCHED)
        queen = _artist(conn, "Queen", MatchStatus.NEEDS_REVIEW)
        in_scope = _song(conn, abba, "Waterloo", MatchStatus.NEEDS_REVIEW)
        outside = _song(conn, queen, "Bohemian Rhapsody", MatchStatus.NEEDS_REVIEW)

        assert PgBroadcastTrackIdentityRepository(conn).rewind_undecided([abba.id]) == 1

        assert _song_row(conn, in_scope)["match_status"] == MatchStatus.PENDING.value
        assert _song_row(conn, outside)["match_status"] == MatchStatus.NEEDS_REVIEW.value


def test_song_rewind_of_no_artists_rewinds_nothing(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        abba = _artist(conn, "ABBA", MatchStatus.AUTO_MATCHED)
        song = _song(conn, abba, "Waterloo", MatchStatus.NEEDS_REVIEW)

        assert PgBroadcastTrackIdentityRepository(conn).rewind_undecided([]) == 0

        assert _song_row(conn, song)["match_status"] == MatchStatus.NEEDS_REVIEW.value


# --- The fan-out sets ----------------------------------------------------------------------


def test_playlists_with_pending_work_come_from_each_side(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        station = _station(conn)
        p_artist, p_song, p_none = (_playlist(conn, station) for _ in range(3))
        pending_artist = _artist(conn, "ABBA", MatchStatus.PENDING)
        _play(conn, p_artist, _song(conn, pending_artist, "Waterloo", MatchStatus.AUTO_MATCHED))
        resolved = _artist(conn, "Queen", MatchStatus.AUTO_MATCHED)
        _play(conn, p_song, _song(conn, resolved, "Bohemian Rhapsody", MatchStatus.PENDING))
        _play(conn, p_none, _song(conn, resolved, "Radio Ga Ga", MatchStatus.AUTO_MATCHED))

        artists = PgBroadcastArtistRepository(conn).playlist_ids_with_pending()
        songs = PgBroadcastTrackIdentityRepository(conn).playlist_ids_with_pending()

        assert artists == {p_artist.id}
        assert songs == {p_song.id}


# --- The cascades clear stale suggestions --------------------------------------------------


def test_bulk_reject_deletes_the_match_rows_of_the_songs_it_flips(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        rejected_artist = _artist(conn, "ABBA", MatchStatus.AUTO_REJECTED)
        flipped = _song(conn, rejected_artist, "Waterloo", MatchStatus.PENDING)
        decided = _song(conn, rejected_artist, "SOS", MatchStatus.MANUAL_MATCHED)
        queen = _artist(conn, "Queen", MatchStatus.PENDING)
        elsewhere = _song(conn, queen, "Bohemian Rhapsody", MatchStatus.PENDING)
        for song in (flipped, decided, elsewhere):
            _suggest(conn, song)

        PgBroadcastTrackIdentityRepository(conn).bulk_reject_by_artist(rejected_artist.id)

        assert _song_row(conn, flipped)["match_status"] == MatchStatus.AUTO_REJECTED.value
        assert _match_rows(conn, flipped) == 0
        assert _match_rows(conn, decided) == 1
        assert _match_rows(conn, elsewhere) == 1


def test_bulk_defer_deletes_the_flipped_songs_rows_and_counts_them(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        deferred_artist = _artist(conn, "ABBA", MatchStatus.NEEDS_REVIEW)
        first = _song(conn, deferred_artist, "Waterloo", MatchStatus.PENDING)
        second = _song(conn, deferred_artist, "SOS", MatchStatus.PENDING)
        decided = _song(conn, deferred_artist, "Fernando", MatchStatus.AUTO_MATCHED)
        for song in (first, second, decided):
            _suggest(conn, song)

        changed = PgBroadcastTrackIdentityRepository(conn).bulk_defer_by_artist(deferred_artist.id)

        assert changed == 2
        for song in (first, second):
            row = _song_row(conn, song)
            assert row["match_status"] == MatchStatus.NEEDS_REVIEW.value
            assert row["reason_code"] == ReasonCode.DEFERRED_RETRY.value
            assert _match_rows(conn, song) == 0
        assert _match_rows(conn, decided) == 1


# --- The replacement -----------------------------------------------------------------------


def test_rematching_a_rewound_song_to_its_kept_file_keeps_one_row(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        playlist = _playlist(conn, _station(conn))
        artist = _artist(conn, "ABBA", MatchStatus.PENDING)
        song = _song(conn, artist, "Waterloo", MatchStatus.NEEDS_REVIEW)
        kept = _file(conn, artist=ABBA, title="Waterloo")
        _suggest(conn, song, kept.id)
        _play(conn, playlist, song)
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

        rows = conn.execute(
            "SELECT library_file_id FROM matches WHERE identity_id = %s", (song.id,)
        ).fetchall()
        assert [r["library_file_id"] for r in rows] == [kept.id]
