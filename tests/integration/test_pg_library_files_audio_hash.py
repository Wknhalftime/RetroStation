"""PG: the audio_hash column, its lookups, and the upsert rules around it."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.enums import AudioHashKind, EnrichmentStatus
from backend.domain.library import AudioHash, LibraryFile

H1 = AudioHash(AudioHashKind.FLAC_MD5, "1" * 32)
H2 = AudioHash(AudioHashKind.AUDIO_SHA256, "2" * 64)


def _row(
    path: str,
    *,
    fmt: str = "flac",
    size: int = 100,
    mtime_ns: int = 1_000,
    audio_hash: AudioHash | None = None,
    enrichment: EnrichmentStatus = EnrichmentStatus.PENDING,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        format=fmt,
        enrichment_status=enrichment,
        file_size=size,
        file_mtime_ns=mtime_ns,
        audio_hash=audio_hash,
    )


def test_audio_hash_round_trips(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/a.flac", audio_hash=H1))
        got = repo.get_by_path("/m/a.flac")

    assert got is not None and got.audio_hash == H1


def test_the_column_refuses_a_malformed_value(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        PgLibraryFileRepository(conn).upsert(_row("/m/a.flac"))
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE library_files SET audio_hash = 'md5:abc'")


def test_audio_hash_indexes_exist(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'library_files'"
            ).fetchall()
        }
    assert {
        "idx_library_files_audio_hash",
        "idx_library_files_audio_unhashed",
        "idx_library_files_stat",
    } <= names


def test_get_by_audio_hash_returns_rows_of_any_status_in_path_order(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/b.flac", audio_hash=H1))
        repo.upsert(_row("/m/a.flac", audio_hash=H1))
        repo.mark_missing("/m/a.flac")
        repo.upsert(_row("/m/c.mp3", fmt="mp3", audio_hash=H2))

        got = [f.file_path for f in repo.get_by_audio_hash(H1)]

    assert got == ["/m/a.flac", "/m/b.flac"]


@pytest.mark.parametrize(
    ("size", "mtime_ns", "incoming", "expected_hash", "expected_enrichment"),
    [
        (100, 1_000, None, H1, EnrichmentStatus.ENRICHED),  # unchanged file
        (101, 1_000, None, None, EnrichmentStatus.PENDING),  # changed: stale hash cleared
        (100, 2_000, None, None, EnrichmentStatus.PENDING),  # retag inside the padding
        (101, 1_000, H2, H2, EnrichmentStatus.PENDING),  # a fresh fingerprint wins
    ],
    ids=["unchanged", "size", "mtime-only", "fresh"],
)
def test_upsert_keeps_the_audio_hash_only_while_the_file_is_unchanged(
    migrated_db: str,
    size: int,
    mtime_ns: int,
    incoming: AudioHash | None,
    expected_hash: AudioHash | None,
    expected_enrichment: EnrichmentStatus,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/a.flac", audio_hash=H1, enrichment=EnrichmentStatus.ENRICHED))

        got = repo.upsert(_row("/m/a.flac", size=size, mtime_ns=mtime_ns, audio_hash=incoming))

    assert got.audio_hash == expected_hash
    assert got.enrichment_status == expected_enrichment


def test_get_by_stat_matches_rows_with_equal_stat_whatever_their_audio_hash_state(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/a.flac", size=10, mtime_ns=5))
        repo.upsert(_row("/m/b.flac", size=10, mtime_ns=6))
        repo.upsert(_row("/m/c.flac", size=10, mtime_ns=5))
        repo.mark_missing("/m/c.flac")
        repo.upsert(_row("/m/d.flac", size=10, mtime_ns=5, audio_hash=H1))

        got = [f.file_path for f in repo.get_by_stat(10, 5)]

    assert got == ["/m/a.flac", "/m/c.flac", "/m/d.flac"]


def test_audio_backlog_is_present_flac_and_mp3_rows_in_path_order(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        for path, fmt in [
            ("/m/c.flac", "flac"),
            ("/m/a.mp3", "mp3"),
            ("/m/b.flac", "flac"),
            ("/m/d.flac", "flac"),
            ("/m/f.ogg", "ogg"),
            ("/m/g.wav", "wav"),
        ]:
            repo.upsert(_row(path, fmt=fmt))
        repo.mark_missing("/m/d.flac")
        repo.upsert(_row("/m/e.flac", audio_hash=H1))

        first = [f.file_path for f in repo.get_audio_unhashed_after(None, 2)]
        rest = [f.file_path for f in repo.get_audio_unhashed_after("/m/b.flac", 2)]
        count = repo.count_audio_unhashed()

    assert first == ["/m/a.mp3", "/m/b.flac"]
    assert rest == ["/m/c.flac"]
    assert count == 3


def test_set_audio_hash_writes_only_over_an_unchanged_row_without_one(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        row = _row("/m/a.flac", size=100, mtime_ns=1_000)
        repo.upsert(row)

        stale_stat = repo.set_audio_hash(row.id, H2, 100, 2_000)
        written = repo.set_audio_hash(row.id, H1, 100, 1_000)
        again = repo.set_audio_hash(row.id, H2, 100, 1_000)
        got = repo.get_by_path("/m/a.flac")

    assert (stale_stat, written, again) == (False, True, False)
    assert got is not None and got.audio_hash == H1


def test_has_any_reports_whether_the_table_has_rows(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        before = repo.has_any()
        repo.upsert(_row("/m/a.flac"))
        after = repo.has_any()
    assert (before, after) == (False, True)


def test_upsert_no_longer_writes_the_whole_file_hash(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        conn.execute(
            "INSERT INTO library_files (file_path, file_hash, format) VALUES (%s, %s, %s)",
            ("/m/old.flac", "h" * 64, "flac"),
        )
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/old.flac"))
        repo.upsert(_row("/m/new.flac"))
        stored = {
            r["file_path"]: r["file_hash"]
            for r in conn.execute("SELECT file_path, file_hash FROM library_files").fetchall()
        }

    assert stored == {"/m/old.flac": "h" * 64, "/m/new.flac": None}
