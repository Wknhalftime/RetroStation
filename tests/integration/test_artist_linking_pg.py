"""Acceptance tests on PostgreSQL: the local-artist linker's storage (AUD-R026, D15).

Migration 0037 adds artists.mb_lookup_at / mb_lookup_outcome, with two CHECKs. The adapter:
- list_due: local artists without an MBID that were never looked up, or that have a present
  file indexed after the lookup, by normalized name;
- evidence: the present files' artist-MBID tag values with their file counts, most files first;
- link: the MBID, origin, MusicBrainz name, sort name and disambiguation, in place, marked
  enhanced and linked. Refused (False, nothing written) when another row holds the MBID or the
  row already has one, so UNIQUE(mbid) is never hit;
- record_outcome: the outcome and the lookup time; never LINKED;
- normalized_names_linked_since: the re-check's second source of names. The targeted re-check
  (scope "changed") rewinds the undecided songs of an artist linked after its watermark.
The rollback script un-links every linked artist and drops the columns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.artist_linking import PgArtistLinkingRepository
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.catalog import ArtistLinkCandidate, InvalidLinkDecisionError
from backend.domain.enums import (
    ArtistLinkOutcome,
    CatalogSource,
    MatchStatus,
    MatchTier,
    ReasonCode,
    TargetType,
    TaskStatus,
    TaskType,
)
from backend.domain.matching import Match
from backend.domain.system import TaskProgress
from backend.services.artist_linking_service import link_artist
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.mb_client import FakeMbClient

Conn = psycopg.Connection[Any]

_DB = Path(__file__).resolve().parents[2] / "backend" / "db"
_VERSION = "0037_artist_mb_lookup"
_ROLLBACK = _DB / f"rollback_{_VERSION}.sql"

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
NIRVANA = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
OTHER = "00000000-0000-4000-8000-000000000042"
LOOKUP_COLUMNS = "('mb_lookup_at', 'mb_lookup_outcome')"


def _connect(url: str) -> Conn:
    return psycopg.connect(url, row_factory=dict_row)


def _local(conn: Conn, name: str) -> str:
    return PgArtistRepository(conn).upsert_local_artist(name, normalize_artist(name))


def _musicbrainz(conn: Conn, name: str, mbid: str) -> str:
    return PgArtistRepository(conn).upsert_musicbrainz_artist(
        mbid=mbid, name=name, sort_name=name, normalized_name=normalize_artist(name)
    )


def _looked_up(
    conn: Conn,
    artist_id: str,
    *,
    at: datetime | None = T0,
    outcome: str | None = "tag_mismatch",
) -> None:
    conn.execute(
        "UPDATE artists SET mb_lookup_at = %s, mb_lookup_outcome = %s WHERE id = %s",
        (at, outcome, artist_id),
    )


def _file(
    conn: Conn,
    artist_name: str,
    *,
    tag: str | None = None,
    present: bool = True,
    indexed_at: datetime = T0 - HOUR,
) -> None:
    conn.execute(
        """INSERT INTO library_files
               (file_path, format, track_title, artist_name, normalized_artist_name,
                artist_mbid, file_status, indexed_at, missing_since)
           VALUES (%s, 'flac', 'Some Song', %s, %s, %s, %s, %s, %s)""",
        (
            f"/music/{uuid4().hex}.flac",
            artist_name,
            normalize_artist(artist_name),
            tag,
            "present" if present else "missing",
            indexed_at,
            None if present else indexed_at,
        ),
    )


def _row(conn: Conn, artist_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM artists WHERE id = %s", (artist_id,)).fetchone()
    assert row is not None
    return dict(row)


def _candidate(mbid: str = NIRVANA, name: str = "Nirvana") -> ArtistLinkCandidate:
    return ArtistLinkCandidate(
        mbid=mbid, name=name, sort_name=name, disambiguation="90s US grunge band"
    )


def _broadcast(conn: Conn, name: str, status: MatchStatus) -> BroadcastArtist:
    repo = PgBroadcastArtistRepository(conn)
    artist = repo.upsert(
        BroadcastArtist(id=uuid4(), original_name=name, normalized_name=normalize_artist(name))
    )
    if status != MatchStatus.PENDING:
        repo.update_match_status(artist.id, status, None, None)
    return artist


def _artist_match(conn: Conn, broadcast: BroadcastArtist, target_id: str) -> None:
    PgMatchRepository(conn).create(
        Match(
            id=uuid4(),
            artist_id=broadcast.id,
            target_id=target_id,
            target_type=TargetType.ARTIST,
            confidence_score=100.0,
            match_tier=MatchTier.NORMALIZATION,
        )
    )


# --- Schema ----------------------------------------------------------------------------------


def test_migration_0037_adds_the_lookup_columns(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (_VERSION,)
        ).fetchone()
        rows = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns"
            f" WHERE table_name = 'artists' AND column_name IN {LOOKUP_COLUMNS}"
        ).fetchall()

    assert applied is not None
    assert {r["column_name"]: r["data_type"] for r in rows} == {
        "mb_lookup_at": "timestamp with time zone",
        "mb_lookup_outcome": "text",
    }


@pytest.mark.parametrize(
    ("at", "outcome"),
    [(T0, "not_found"), (None, "tag_mismatch"), (T0, None)],
)
def test_the_database_refuses_an_inconsistent_lookup_stamp(
    migrated_db: str, at: datetime | None, outcome: str | None
) -> None:
    with _connect(migrated_db) as conn:
        artist_id = _local(conn, "Yes")
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            _looked_up(conn, artist_id, at=at, outcome=outcome)


@pytest.mark.parametrize("outcome", [o.value for o in ArtistLinkOutcome if o.value != "linked"])
def test_the_database_accepts_every_recorded_outcome(migrated_db: str, outcome: str) -> None:
    with _connect(migrated_db) as conn:
        artist_id = _local(conn, "Yes")
        _looked_up(conn, artist_id, outcome=outcome)

        assert _row(conn, artist_id)["mb_lookup_outcome"] == outcome


def test_the_repository_factory_builds_the_adapter(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        assert isinstance(RepositoryFactory(conn).artist_linking, PgArtistLinkingRepository)


# --- list_due --------------------------------------------------------------------------------


def test_due_artists_are_local_unlinked_and_not_looked_up_since_their_last_file(
    migrated_db: str,
) -> None:
    with _connect(migrated_db) as conn:
        never = _local(conn, "Yes")
        _looked_up(conn, _local(conn, "Boston"))
        refreshed = _local(conn, "Chicago")
        _looked_up(conn, refreshed)
        _file(conn, "Chicago", indexed_at=T0 + HOUR)
        gone = _local(conn, "Genesis")
        _looked_up(conn, gone)
        _file(conn, "Genesis", present=False, indexed_at=T0 + HOUR)
        old = _local(conn, "America")
        _looked_up(conn, old)
        _file(conn, "America", indexed_at=T0 - HOUR)
        same_instant = _local(conn, "Kansas")
        _looked_up(conn, same_instant)
        _file(conn, "Kansas", indexed_at=T0)
        _musicbrainz(conn, "Nirvana", NIRVANA)
        linked = _local(conn, "Soundgarden")
        PgArtistLinkingRepository(conn).link(linked, _candidate(OTHER, "Soundgarden"))

        due = PgArtistLinkingRepository(conn).list_due()

    assert [a.id for a in due] == [refreshed, never]  # by normalized name
    assert all(a.origin == CatalogSource.LOCAL and a.mbid is None for a in due)


# --- evidence --------------------------------------------------------------------------------


def test_evidence_counts_each_tag_value_on_present_files_most_files_first(
    migrated_db: str,
) -> None:
    a = "00000000-0000-4000-8000-000000000001"
    b = "00000000-0000-4000-8000-000000000002"
    c = "00000000-0000-4000-8000-000000000003"
    with _connect(migrated_db) as conn:
        _local(conn, "Soundgarden")
        _file(conn, "Soundgarden", tag=a)
        _file(conn, "Soundgarden", tag=a)
        _file(conn, "Soundgarden")  # untagged
        _file(conn, "Soundgarden", tag=f"{b}, {a}")
        _file(conn, "Soundgarden", tag=c, present=False)  # missing: no evidence
        _file(conn, "Chris Cornell", tag=c)  # another artist's file

        evidence = PgArtistLinkingRepository(conn).evidence("soundgarden")

    assert evidence.tag_counts == ((a, 2), (f"{b}, {a}", 1))


# --- link ------------------------------------------------------------------------------------


def test_link_gives_the_artist_its_mbid_in_place(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        artist_id = _local(conn, "NIRVANA")
        work_id = PgWorkRepository(conn).create_local("Lithium", artist_id)
        broadcast = _broadcast(conn, "NIRVANA", MatchStatus.AUTO_MATCHED)
        _artist_match(conn, broadcast, artist_id)

        written = PgArtistLinkingRepository(conn).link(artist_id, _candidate())

        row = _row(conn, artist_id)
        work = conn.execute("SELECT artist_id FROM works WHERE id = %s", (work_id,)).fetchone()
        targets = conn.execute(
            "SELECT target_id FROM matches WHERE artist_id = %s", (broadcast.id,)
        ).fetchall()

    assert written is True
    kept = (
        "id",
        "mbid",
        "origin",
        "name",
        "sort_name",
        "disambiguation",
        "normalized_name",
        "needs_enhancement",
        "enhancement_error",
        "mb_lookup_outcome",
    )
    assert {key: row[key] for key in kept} == {
        "id": artist_id,
        "mbid": NIRVANA,
        "origin": "musicbrainz",
        "name": "Nirvana",
        "sort_name": "Nirvana",
        "disambiguation": "90s US grunge band",
        "normalized_name": "nirvana",
        "needs_enhancement": False,
        "enhancement_error": None,
        "mb_lookup_outcome": "linked",
    }
    assert row["enhanced_at"] is not None
    assert row["mb_lookup_at"] is not None
    assert work is not None
    assert work["artist_id"] == artist_id
    assert [t["target_id"] for t in targets] == [artist_id]


def test_link_is_refused_when_another_artist_holds_the_mbid(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        holder = _musicbrainz(conn, "Nirvana (US)", NIRVANA)  # normalizes to "nirvana us"
        artist_id = _local(conn, "Nirvana")
        before = _row(conn, holder)

        written = PgArtistLinkingRepository(conn).link(artist_id, _candidate())

        local_row = _row(conn, artist_id)
        holder_row = _row(conn, holder)

    assert written is False
    assert (local_row["mbid"], local_row["origin"], local_row["mb_lookup_outcome"]) == (
        None,
        "local",
        None,
    )
    assert holder_row == before


def test_link_is_refused_for_an_artist_that_already_has_an_mbid(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        artist_id = _musicbrainz(conn, "Nirvana", NIRVANA)

        written = PgArtistLinkingRepository(conn).link(artist_id, _candidate(OTHER))

        row = _row(conn, artist_id)

    assert written is False
    assert row["mbid"] == NIRVANA


def test_the_catalog_reads_the_lookup_fields(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        artist_id = _local(conn, "Nirvana")
        PgArtistLinkingRepository(conn).link(artist_id, _candidate())

        artist = PgArtistRepository(conn).get_by_id(artist_id)

    assert artist is not None
    assert artist.mb_lookup_outcome == ArtistLinkOutcome.LINKED
    assert artist.mb_lookup_at is not None
    assert artist.linked_by_lookup is True
    assert artist.origin == CatalogSource.MUSICBRAINZ


# --- record_outcome --------------------------------------------------------------------------


def test_record_outcome_stamps_the_lookup_and_refuses_linked(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        artist_id = _local(conn, "Boston")
        repo = PgArtistLinkingRepository(conn)

        repo.record_outcome(artist_id, ArtistLinkOutcome.AMBIGUOUS)
        row = _row(conn, artist_id)
        with pytest.raises(InvalidLinkDecisionError):
            repo.record_outcome(artist_id, ArtistLinkOutcome.LINKED)

    assert row["mb_lookup_outcome"] == "ambiguous"
    assert row["mb_lookup_at"] is not None
    assert row["mbid"] is None


# --- The service against PostgreSQL ----------------------------------------------------------


def test_link_artist_on_postgres_links_the_tagged_mbid(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        artist_id = _local(conn, "NIRVANA")
        _file(conn, "NIRVANA", tag=NIRVANA)
        artist = PgArtistRepository(conn).get_by_id(artist_id)
        assert artist is not None
        mb = FakeMbClient(artists={NIRVANA: {"id": NIRVANA, "name": "Nirvana"}})

        outcome = link_artist(artist, PgArtistLinkingRepository(conn), mb)

        row = _row(conn, artist_id)

    assert outcome == ArtistLinkOutcome.LINKED
    assert (row["mbid"], row["mb_lookup_outcome"]) == (NIRVANA, "linked")


# --- The re-check's names --------------------------------------------------------------------


def test_names_linked_since_hold_the_catalog_name_and_matched_broadcast_names(
    migrated_db: str,
) -> None:
    with _connect(migrated_db) as conn:
        linked = _local(conn, "Nirvana")
        exact = _broadcast(conn, "NIRVANA", MatchStatus.AUTO_MATCHED)
        manual = _broadcast(conn, "NIRVANA (UK BAND)", MatchStatus.MANUAL_MATCHED)
        _artist_match(conn, exact, linked)  # a pre-link match: the local id
        PgArtistLinkingRepository(conn).link(linked, _candidate())
        _artist_match(conn, manual, NIRVANA)  # a post-link match: the MBID
        earlier = _local(conn, "Yes")
        PgArtistLinkingRepository(conn).link(earlier, _candidate(OTHER, "Yes"))
        conn.execute("UPDATE artists SET mb_lookup_at = %s WHERE id = %s", (T0 - HOUR, earlier))
        _looked_up(conn, _local(conn, "Boston"), at=T0 + HOUR)  # stamped, not linked

        names = PgArtistLinkingRepository(conn).normalized_names_linked_since(T0)

    assert names == {"nirvana", "nirvana uk band"}


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


def _song(conn: Conn, artist: BroadcastArtist, title: str) -> BroadcastTrackIdentity:
    repo = PgBroadcastTrackIdentityRepository(conn)
    norm = normalize_title(title)
    song = repo.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title=title,
            normalized_title=norm,
            normalized_signature=compute_normalized_signature(artist.normalized_name, norm),
        )
    )
    repo.update_match_status(
        song.id,
        MatchStatus.NEEDS_REVIEW,
        MatchTier.LOCAL_FILE_FUZZY,
        ReasonCode.LOW_CONFIDENCE,
        "stale",
    )
    return song


def _played(conn: Conn, playlist: BroadcastPlaylist, song: BroadcastTrackIdentity) -> None:
    PgBroadcastPlayEventRepository(conn).create(
        BroadcastPlayEvent(
            id=uuid4(), identity_id=song.id, playlist_id=playlist.id, played_at=datetime.now(UTC)
        )
    )


def _status(conn: Conn, song: BroadcastTrackIdentity) -> str:
    row = conn.execute(
        "SELECT match_status FROM track_identities WHERE id = %s", (song.id,)
    ).fetchone()
    assert row is not None
    return str(row["match_status"])


def test_the_changed_recheck_rewinds_the_songs_of_an_artist_linked_after_its_watermark(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    with _connect(migrated_db) as conn:
        PgTaskProgressRepository(conn).upsert(
            TaskProgress(
                task_id=uuid4().hex,
                task_type=TaskType.MATCHING_RECHECK,
                status=TaskStatus.COMPLETED,
                progress_data={},
                started_at=T0,
                updated_at=T0,
                completed_at=T0,
            )
        )
        playlist = _playlist(conn, _station(conn))
        linked = _local(conn, "Nirvana")
        nirvana = _broadcast(conn, "NIRVANA", MatchStatus.AUTO_MATCHED)
        _artist_match(conn, nirvana, linked)
        lithium = _song(conn, nirvana, "Lithium")
        _played(conn, playlist, lithium)
        PgArtistLinkingRepository(conn).link(linked, _candidate())  # now(): after T0
        unlinked = _local(conn, "Boston")
        boston = _broadcast(conn, "BOSTON", MatchStatus.AUTO_MATCHED)
        _artist_match(conn, boston, unlinked)
        more_than = _song(conn, boston, "More Than a Feeling")
        _played(conn, playlist, more_than)
    monkeypatch.setattr(
        "backend.tasks.artist_matching_tasks.artist_matching_task", lambda playlist_id: None
    )
    try:
        from backend.tasks.matching_recheck_tasks import rematch_undecided_task

        summary = rematch_undecided_task.call_local("changed")
    finally:
        get_settings.cache_clear()

    with _connect(migrated_db) as conn:
        assert _status(conn, lithium) == MatchStatus.PENDING.value
        assert _status(conn, more_than) == MatchStatus.NEEDS_REVIEW.value  # not in the wave
    assert summary["songs_rewound"] == 1


# --- Rollback --------------------------------------------------------------------------------


def test_the_rollback_script_unlinks_and_drops_the_columns(migrated_db: str) -> None:
    conn = _connect(migrated_db)
    try:
        linked = _local(conn, "Nirvana")
        PgArtistLinkingRepository(conn).link(linked, _candidate())

        conn.execute(_ROLLBACK.read_text(encoding="utf-8"))

        row = _row(conn, linked)
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            f" WHERE table_name = 'artists' AND column_name IN {LOOKUP_COLUMNS}"
        ).fetchall()
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (_VERSION,)
        ).fetchone()
    finally:
        conn.rollback()  # DDL is transactional: the test database keeps 0037
        conn.close()

    assert (row["mbid"], row["origin"], row["disambiguation"]) == (None, "local", None)
    assert "mb_lookup_outcome" not in row
    assert columns == []
    assert applied is None
