from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_quarantine import PgLibraryQuarantineRepository
from backend.domain.enums import EnrichmentStatus, ReleaseStatus, ReleaseType
from backend.domain.library import AudioMetadata, LibraryFile, LibraryQuarantine


def _make_file(
    *,
    file_path: str = "/music/track.flac",
    format: str = "flac",
    artist_mbid: str | None = None,
    album_artist_mbid: str | None = None,
    recording_mbid: str | None = None,
    release_mbid: str | None = None,
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=file_path,
        file_hash="abc123",
        format=format,
        enrichment_status=enrichment_status,
        audio=AudioMetadata(
            artist_mbid=artist_mbid,
            album_artist_mbid=album_artist_mbid,
            recording_mbid=recording_mbid,
            release_mbid=release_mbid,
            release_title="Test Album",
            release_type=ReleaseType.ALBUM,
            release_status=ReleaseStatus.OFFICIAL,
            track_title="Test Track",
            track_number=1,
            disc_number=1,
            duration_ms=240000,
            bitrate=320,
            raw_metadata={"title": "Test Track"},
        ),
    )


def test_library_file_upsert_and_get(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        lf = _make_file(file_path="/music/song.flac", format="flac")
        created = repo.upsert(lf)

        assert created.file_path == "/music/song.flac"
        assert created.format == "flac"
        assert created.enrichment_status == EnrichmentStatus.PENDING
        assert created.audio.release_type == ReleaseType.ALBUM
        assert created.audio.release_status == ReleaseStatus.OFFICIAL
        assert created.audio.raw_metadata == {"title": "Test Track"}
        assert created.audio.bitrate == 320
        assert created.audio.duration_ms == 240000

        # get_by_id
        fetched = repo.get_by_id(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.audio.track_title == "Test Track"

        # get_by_path
        by_path = repo.get_by_path("/music/song.flac")
        assert by_path is not None
        assert by_path.id == created.id

        # Re-upsert same path with updated metadata — should overwrite
        lf2 = LibraryFile(
            id=uuid4(),
            file_path="/music/song.flac",
            file_hash="newhashabc",
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
            audio=AudioMetadata(
                track_title="Updated Title",
                bitrate=256,
            ),
        )
        updated = repo.upsert(lf2)
        assert updated.id == created.id  # same row
        assert updated.file_hash == "newhashabc"
        assert updated.audio.track_title == "Updated Title"
        assert updated.audio.bitrate == 256

        conn.commit()


def test_library_file_get_by_artist_mbid(migrated_db: str) -> None:
    mbid = "artist-mbid-001"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        # 2 files with artist_mbid, 1 with album_artist_mbid
        f1 = _make_file(file_path="/a/1.flac", artist_mbid=mbid)
        f2 = _make_file(file_path="/a/2.flac", artist_mbid=mbid)
        f3 = _make_file(file_path="/a/3.flac", album_artist_mbid=mbid)
        # unrelated
        f4 = _make_file(file_path="/a/4.flac", artist_mbid="other-mbid")

        for f in (f1, f2, f3, f4):
            repo.upsert(f)

        results = repo.get_by_artist_mbid(mbid)
        paths = {r.file_path for r in results}
        assert paths == {"/a/1.flac", "/a/2.flac", "/a/3.flac"}

        conn.commit()


def test_library_file_pending_enrichment_queries(migrated_db: str) -> None:
    release_mbid = "release-mbid-001"
    recording_mbid = "recording-mbid-001"

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        # 2 pending files with the release_mbid
        r1 = _make_file(
            file_path="/b/1.flac",
            release_mbid=release_mbid,
            recording_mbid=recording_mbid,
            enrichment_status=EnrichmentStatus.PENDING,
        )
        r2 = _make_file(
            file_path="/b/2.flac",
            release_mbid=release_mbid,
            enrichment_status=EnrichmentStatus.PENDING,
        )
        # already enriched — should NOT appear
        r3 = _make_file(
            file_path="/b/3.flac",
            release_mbid=release_mbid,
            enrichment_status=EnrichmentStatus.ENRICHED,
        )
        # has recording_mbid but NO release_mbid — for get_pending_by_recording
        r4 = _make_file(
            file_path="/b/4.flac",
            recording_mbid=recording_mbid,
            enrichment_status=EnrichmentStatus.PENDING,
        )

        for f in (r1, r2, r3, r4):
            repo.upsert(f)

        by_release = repo.get_pending_enrichment_by_release(release_mbid)
        assert len(by_release) == 2
        assert {f.file_path for f in by_release} == {"/b/1.flac", "/b/2.flac"}

        # get_pending_enrichment_by_recording excludes files that have a release_mbid
        by_recording = repo.get_pending_enrichment_by_recording(recording_mbid)
        assert len(by_recording) == 1
        assert by_recording[0].file_path == "/b/4.flac"

        conn.commit()


def test_library_file_update_recording_link(migrated_db: str) -> None:
    rec_mbid = "rec-mbid-link-test-0001"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        # Insert a minimal recording row to satisfy the FK
        conn.execute(
            "INSERT INTO recordings (id, title) VALUES (%s, %s)",
            (rec_mbid, "Test Recording"),
        )

        repo = PgLibraryFileRepository(conn)

        lf = _make_file(file_path="/c/track.mp3", format="mp3")
        created = repo.upsert(lf)
        assert created.recording_id is None
        assert created.enrichment_status == EnrichmentStatus.PENDING

        repo.update_recording_link(created.id, rec_mbid, EnrichmentStatus.ENRICHED)

        updated = repo.get_by_id(created.id)
        assert updated is not None
        assert updated.recording_id == rec_mbid
        assert updated.enrichment_status == EnrichmentStatus.ENRICHED

        conn.commit()


def test_library_file_count_aggregates(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        repo.upsert(_make_file(file_path="/d/1.flac", format="flac"))
        repo.upsert(_make_file(file_path="/d/2.flac", format="flac"))
        repo.upsert(
            _make_file(
                file_path="/d/3.mp3",
                format="mp3",
                enrichment_status=EnrichmentStatus.ENRICHED,
            )
        )

        by_format = repo.count_by_format()
        assert by_format.get("flac") == 2
        assert by_format.get("mp3") == 1

        by_status = repo.count_by_enrichment_status()
        assert by_status.get(EnrichmentStatus.PENDING.value) == 2
        assert by_status.get(EnrichmentStatus.ENRICHED.value) == 1

        conn.commit()


def test_quarantine_create_and_list(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryQuarantineRepository(conn)

        entry = LibraryQuarantine(
            id=uuid4(),
            file_path="/bad/corrupt.mp3",
            error_message="mutagen could not identify file",
            trace_id="trace-001",
        )
        created = repo.create(entry)
        assert created.file_path == "/bad/corrupt.mp3"
        assert created.error_message == "mutagen could not identify file"
        assert created.trace_id == "trace-001"

        all_entries = repo.list_all()
        assert len(all_entries) == 1
        assert all_entries[0].file_path == "/bad/corrupt.mp3"

        by_path = repo.get_by_path("/bad/corrupt.mp3")
        assert by_path is not None
        assert by_path.id == created.id

        # non-existent path returns None
        assert repo.get_by_path("/does/not/exist.mp3") is None

        conn.commit()


# ---------------------------------------------------------------------------
# Write-only methods (incremental scan performance paths)
# ---------------------------------------------------------------------------


def test_upsert_write_only_inserts_and_is_retrievable(migrated_db: str) -> None:
    """upsert_write_only should insert a row retrievable by get_by_path."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        lf = _make_file(file_path="/music/write_only.flac", format="flac")
        repo.upsert_write_only(lf)
        conn.commit()

        result = repo.get_by_path("/music/write_only.flac")
        assert result is not None
        assert result.id == lf.id
        assert result.file_hash == lf.file_hash
        assert result.format == "flac"
        assert result.audio.track_title == "Test Track"


def test_upsert_write_only_updates_existing_row(migrated_db: str) -> None:
    """upsert_write_only on same path should update, not duplicate."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)

        lf1 = _make_file(file_path="/music/update_me.flac", format="flac")
        repo.upsert_write_only(lf1)
        conn.commit()

        lf2 = _make_file(file_path="/music/update_me.flac", format="mp3")
        lf2.file_hash = "updated_hash"
        lf2.audio.track_title = "Updated Title"
        repo.upsert_write_only(lf2)
        conn.commit()

        result = repo.get_by_path("/music/update_me.flac")
        assert result is not None
        assert result.format == "mp3"
        assert result.file_hash == "updated_hash"
        assert result.audio.track_title == "Updated Title"


def test_create_write_only_inserts_quarantine(migrated_db: str) -> None:
    """create_write_only should insert a retrievable quarantine entry."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryQuarantineRepository(conn)

        entry = LibraryQuarantine(
            id=uuid4(),
            file_path="/music/bad_file.mp3",
            error_message="Corrupt header",
        )
        repo.create_write_only(entry)
        conn.commit()

        result = repo.get_by_path("/music/bad_file.mp3")
        assert result is not None
        assert result.id == entry.id
        assert result.error_message == "Corrupt header"


def test_create_write_only_appears_in_list_all(migrated_db: str) -> None:
    """Entries from create_write_only should appear in list_all."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryQuarantineRepository(conn)

        entry = LibraryQuarantine(
            id=uuid4(),
            file_path="/music/bad2.mp3",
            error_message="Truncated",
        )
        repo.create_write_only(entry)
        conn.commit()

        all_entries = repo.list_all()
        assert any(e.file_path == "/music/bad2.mp3" for e in all_entries)
