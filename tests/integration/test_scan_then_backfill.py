"""Integration: a scan reads tags only, and the backfill completes the fingerprints.

A scan never reads a file past its tags, whether the library is empty or
not; a FLAC's stored MD5 comes with its tags. library_hash_backfill_task then
fingerprints what a scan could not: MP3s and FLACs without a stored MD5.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.enums import EnrichmentStatus
from backend.domain.library import LibraryFile
from backend.services.audio_hash import compute_audio_hash
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_hash_backfill_tasks import BackfillRunConfig, run_hash_backfill
from backend.tasks.library_scan_tasks import _run_scan
from tests.fixtures.audio_builders import tag_flac, write_flac

pytestmark = pytest.mark.integration

_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"

Conn = psycopg.Connection[dict[str, Any]]


def _put(src_name: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_AUDIO / src_name, dest)
    return dest


def _flac(path: Path, *, store_md5: bool) -> Path:
    write_flac(path, [(300, -300), (400, -400)], store_md5=store_md5)
    tag_flac(path, {"artist": "Prince", "title": "Kiss" if store_md5 else "Sometimes It Snows"})
    return path


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """MP3s (two byte-identical copies), an OGG, a WAV, a corrupt file, two FLACs."""
    root = tmp_path / "lib"
    _put("well_tagged.mp3", root / "a" / "kiss.mp3")
    _put("well_tagged.mp3", root / "b" / "kiss.mp3")
    _put("partial_tags.mp3", root / "a" / "partial.mp3")
    _put("minimal_tags.ogg", root / "c" / "minimal.ogg")
    _put("no_tags.wav", root / "c" / "no_tags.wav")
    _put("corrupt.mp3", root / "c" / "corrupt.mp3")
    _flac(root / "d" / "md5.flac", store_md5=True)
    _flac(root / "d" / "no_md5.flac", store_md5=False)
    return root


def _scan(conn: Conn, root: Path) -> None:
    _run_scan(
        root_path=str(root),
        library_conn=conn,
        repos=RepositoryFactory(conn),
        progress_repo=PgTaskProgressRepository(conn),
        task_id=uuid4().hex,
    )
    conn.commit()


def _drain_backfill(conn: Conn) -> None:
    repos = RepositoryFactory(conn)
    run_hash_backfill(
        repos.library_files,
        repos.task_progress,
        conn.commit,
        BackfillRunConfig(run_id=uuid4().hex),
    )


def _hashes(conn: Conn, root: Path) -> dict[str, str | None]:
    rows = conn.execute(
        "SELECT file_path, audio_hash FROM library_files WHERE starts_with(file_path, %s)",
        (str(root),),
    ).fetchall()
    return {Path(r["file_path"]).relative_to(root).as_posix(): r["audio_hash"] for r in rows}


def _snapshot(conn: Conn, root: Path) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        """SELECT f.file_path, f.audio_hash, f.format, f.enrichment_status, f.file_status,
                  f.track_title, f.artist_name, f.file_size, f.file_mtime_ns,
                  w.title AS work, a.name AS work_artist
             FROM library_files f
             LEFT JOIN works w ON w.id = f.work_id
             LEFT JOIN artists a ON a.id = w.artist_id
            WHERE starts_with(f.file_path, %s)
            ORDER BY f.file_path""",
        (str(root),),
    ).fetchall()
    return [tuple(r.values()) for r in rows]


def test_a_scan_fingerprints_only_flacs_that_store_an_md5(migrated_db: str, library: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _scan(conn, library)
        hashes = _hashes(conn, library)

    assert len(hashes) == 7  # the corrupt file is quarantined, not a row
    assert hashes["d/md5.flac"] == str(compute_audio_hash(library / "d" / "md5.flac"))
    assert all(v is None for k, v in hashes.items() if k != "d/md5.flac")


def test_the_backfill_fingerprints_the_rest(migrated_db: str, library: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _scan(conn, library)
        _drain_backfill(conn)
        hashes = _hashes(conn, library)

    for name in ("a/kiss.mp3", "b/kiss.mp3", "a/partial.mp3", "d/md5.flac", "d/no_md5.flac"):
        assert hashes[name] == str(compute_audio_hash(library / name)), name
    assert hashes["a/kiss.mp3"] == hashes["b/kiss.mp3"]  # bit-identical copies share one
    assert (hashes["c/minimal.ogg"], hashes["c/no_tags.wav"]) == (None, None)


def test_empty_and_non_empty_libraries_scan_to_the_same_rows(
    migrated_db: str, library: Path
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _scan(conn, library)
        into_empty = _snapshot(conn, library)

        conn.execute(
            """TRUNCATE library_files, library_quarantine, library_folders, works,
                        recordings, artists, song_masters, progress_tracking CASCADE"""
        )
        RepositoryFactory(conn).library_files.upsert(
            LibraryFile(
                id=uuid4(),
                file_path=str(library.parent / "elsewhere" / "x.flac"),
                file_hash=None,
                format="flac",
            )
        )
        conn.commit()
        _scan(conn, library)
        into_non_empty = _snapshot(conn, library)

    assert into_empty == into_non_empty


def test_rescan_keeps_enrichment_and_backfilled_fingerprints(
    migrated_db: str, library: Path
) -> None:
    kiss = library / "a" / "kiss.mp3"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _scan(conn, library)
        _drain_backfill(conn)
        row = repos.library_files.get_by_path(str(kiss))
        assert row is not None and row.audio_hash is not None
        repos.library_files.update_recording_link(
            row.id, row.recording_id, EnrichmentStatus.ENRICHED
        )
        conn.commit()

        _scan(conn, library)

        after = repos.library_files.get_by_path(str(kiss))

    assert after is not None
    assert after.audio_hash == row.audio_hash
    assert after.enrichment_status == EnrichmentStatus.ENRICHED


def test_a_rescan_hashes_no_audio_bytes(
    migrated_db: str, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _scan(conn, library)
        monkeypatch.setattr(
            "backend.services.audio_hash._sha256_of_range",
            lambda *_a: pytest.fail("a scan hashed audio bytes"),
        )
        _scan(conn, library)
