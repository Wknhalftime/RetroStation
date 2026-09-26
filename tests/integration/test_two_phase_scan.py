"""Integration: a first scan defers hashes, and the backfill completes the library.

The quality bar: phase 1 plus a complete backfill leaves exactly the library
a one-phase scan of the same files builds.
"""
from __future__ import annotations

import hashlib
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
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_hash_backfill_tasks import BackfillRunConfig, run_hash_backfill
from backend.tasks.library_scan_tasks import _run_scan

pytestmark = pytest.mark.integration

_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"

Conn = psycopg.Connection[dict[str, Any]]


def _put(src_name: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_AUDIO / src_name, dest)
    return dest


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """Tagged, untagged and corrupt files, plus two byte-identical copies."""
    root = tmp_path / "lib"
    _put("well_tagged.mp3", root / "a" / "kiss.mp3")
    _put("well_tagged.mp3", root / "b" / "kiss.mp3")
    _put("partial_tags.mp3", root / "a" / "partial.mp3")
    _put("minimal_tags.ogg", root / "c" / "minimal.ogg")
    _put("no_tags.wav", root / "c" / "no_tags.wav")
    _put("corrupt.mp3", root / "c" / "corrupt.mp3")
    return root


def _scan(conn: Conn, root: Path) -> None:
    _run_scan(
        root_path=str(root), library_conn=conn, repos=RepositoryFactory(conn),
        progress_repo=PgTaskProgressRepository(conn), task_id=uuid4().hex,
    )
    conn.commit()


def _drain_backfill(conn: Conn) -> None:
    repos = RepositoryFactory(conn)
    run_hash_backfill(
        repos.library_files, repos.task_progress, conn.commit,
        BackfillRunConfig(run_id=uuid4().hex),
    )


def _snapshot(conn: Conn, root: Path) -> dict[str, list[tuple[Any, ...]]]:
    prefix = str(root)
    files = conn.execute(
        """SELECT f.file_path, f.file_hash, f.format, f.enrichment_status, f.file_status,
                  f.track_title, f.artist_name, f.file_size, f.file_mtime_ns,
                  w.title AS work, a.name AS work_artist,
                  r.title AS recording, r.version_type,
                  (sm.preferred_file_id = f.id) AS preferred
             FROM library_files f
             LEFT JOIN works w ON w.id = f.work_id
             LEFT JOIN artists a ON a.id = w.artist_id
             LEFT JOIN recordings r ON r.id = f.recording_id
             LEFT JOIN song_masters sm ON sm.work_id = f.work_id
            WHERE starts_with(f.file_path, %s)
            ORDER BY f.file_path""",
        (prefix,),
    ).fetchall()
    quarantine = conn.execute(
        """SELECT file_path, error_message FROM library_quarantine
            WHERE starts_with(file_path, %s) ORDER BY file_path""",
        (prefix,),
    ).fetchall()
    folders = conn.execute(
        """SELECT full_path, folder_hash FROM library_folders
            WHERE starts_with(full_path, %s) ORDER BY full_path""",
        (prefix,),
    ).fetchall()
    return {
        "files": [tuple(r.values()) for r in files],
        "quarantine": [tuple(r.values()) for r in quarantine],
        "folders": [tuple(r.values()) for r in folders],
    }


def _truncate_library(conn: Conn) -> None:
    conn.execute(
        """TRUNCATE library_files, library_quarantine, library_folders, works,
                    recordings, artists, song_masters, progress_tracking CASCADE"""
    )
    conn.commit()


def test_first_scan_defers_hashes_but_groups_every_tagged_file(
    migrated_db: str, library: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _scan(conn, library)
        rows = conn.execute(
            "SELECT file_hash, file_size, artist_name, track_title, work_id FROM library_files"
        ).fetchall()

    assert len(rows) == 5
    assert all(r["file_hash"] is None and r["file_size"] is not None for r in rows)
    tagged = [r for r in rows if r["artist_name"] and r["track_title"]]
    assert tagged
    assert all(r["work_id"] is not None for r in tagged)


def test_backfill_after_first_scan_matches_a_one_phase_scan(
    migrated_db: str, library: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _scan(conn, library)
        _drain_backfill(conn)
        two_phase = _snapshot(conn, library)

        # One-phase baseline over the same files: a row outside the library
        # makes the table non-empty, so the scan hashes inline.
        _truncate_library(conn)
        RepositoryFactory(conn).library_files.upsert(LibraryFile(
            id=uuid4(), file_path=str(library.parent / "elsewhere" / "x.flac"),
            file_hash="0" * 64, format="flac",
        ))
        conn.commit()
        _scan(conn, library)
        one_phase = _snapshot(conn, library)

    assert all(row[1] is not None for row in two_phase["files"])
    assert len(two_phase["files"]) == 5
    assert len(two_phase["quarantine"]) == 1
    assert two_phase == one_phase


def test_rescan_after_first_scan_keeps_enrichment_and_fills_hashes(
    migrated_db: str, library: Path,
) -> None:
    kiss = library / "a" / "kiss.mp3"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _scan(conn, library)
        row = repos.library_files.get_by_path(str(kiss))
        assert row is not None
        repos.library_files.update_recording_link(
            row.id, row.recording_id, EnrichmentStatus.ENRICHED,
        )
        conn.commit()

        _scan(conn, library)

        after = repos.library_files.get_by_path(str(kiss))
        count = conn.execute("SELECT COUNT(*) AS n FROM library_files").fetchone()

    assert after is not None
    assert after.file_hash == hashlib.sha256(kiss.read_bytes()).hexdigest()
    assert after.enrichment_status == EnrichmentStatus.ENRICHED
    assert count is not None and count["n"] == 5


def test_scan_after_interrupted_first_scan_hashes_leftovers(
    migrated_db: str, library: Path,
) -> None:
    partial = library / "a" / "partial.mp3"
    st = partial.stat()
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        # What a first scan killed after one file leaves behind.
        repos.library_files.upsert(LibraryFile(
            id=uuid4(), file_path=str(partial), file_hash=None, format="mp3",
            file_size=st.st_size, file_mtime_ns=st.st_mtime_ns,
        ))
        conn.commit()

        _scan(conn, library)

        rows = conn.execute("SELECT file_path, file_hash FROM library_files").fetchall()

    assert len(rows) == 5
    assert all(r["file_hash"] is not None for r in rows)
