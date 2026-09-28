"""Integration: a full rescan groups the stored rows, not the ids its fresh reads carry.

Every file a full scan reads gets a fresh id; the upsert keys on the path,
so a file already indexed keeps its stored id and the fresh one exists
nowhere. Files a scan cannot fingerprint (MP3s, FLACs without a stored MD5)
reach title matching on every rescan, and a retag that no longer matches
their work used to create a song master pointing at the fresh id.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.library import LibraryFile
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_scan_tasks import _run_scan
from tests.fixtures.audio_builders import tag_flac, write_flac

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]


def _unfingerprinted_flac(path: Path, title: str, level: int) -> Path:
    """A FLAC without a stored MD5: a scan leaves it with no audio hash."""
    write_flac(path, [(level, -level)], store_md5=False)
    tag_flac(path, {"artist": "Prince", "title": title})
    return path


def _full_scan(conn: Conn, root: Path) -> None:
    _run_scan(
        root_path=str(root),
        library_conn=conn,
        repos=RepositoryFactory(conn),
        progress_repo=PgTaskProgressRepository(conn),
        task_id=uuid4().hex,
    )
    conn.commit()


def _stored(repos: RepositoryFactory, path: Path) -> LibraryFile:
    lf = repos.library_files.get_by_path(str(path))
    assert lf is not None
    return lf


def _work_exists(conn: Conn, work_id: str | None) -> bool:
    row = conn.execute("SELECT 1 FROM works WHERE id = %s", (work_id,)).fetchone()
    return row is not None


def test_rescan_of_a_retagged_unfingerprinted_file_keeps_the_chunk_grouped(
    migrated_db: str, tmp_path: Path
) -> None:
    retagged = _unfingerprinted_flac(tmp_path / "b" / "kiss.flac", "Kiss", 300)
    unchanged = _unfingerprinted_flac(tmp_path / "c" / "raspberry.flac", "Raspberry Beret", 500)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        retagged_before = _stored(repos, retagged)
        unchanged_before = _stored(repos, unchanged)
        assert retagged_before.audio_hash is None
        assert retagged_before.work_id is not None

        # Grouped in the same chunk as the retagged file, before it.
        added = _unfingerprinted_flac(tmp_path / "a" / "alphabet.flac", "Alphabet St.", 700)
        tag_flac(retagged, {"title": "Sometimes It Snows in April"})
        _full_scan(conn, tmp_path)

        retagged_after = _stored(repos, retagged)
        unchanged_after = _stored(repos, unchanged)
        added_after = _stored(repos, added)
        retagged_work_exists = _work_exists(conn, retagged_after.work_id)
        added_work_exists = _work_exists(conn, added_after.work_id)

    assert added_after.work_id is not None and added_work_exists
    # A retag is not a new song: the upsert keeps the row's work.
    assert retagged_after.id == retagged_before.id
    assert retagged_after.work_id == retagged_before.work_id and retagged_work_exists
    assert unchanged_after.work_id == unchanged_before.work_id


def test_rescan_groups_the_stored_row_of_a_file_that_had_no_work(
    migrated_db: str, tmp_path: Path
) -> None:
    """A file indexed without an artist gets its work on the rescan after it is tagged."""
    path = tmp_path / "kiss.flac"
    write_flac(path, [(300, -300)], store_md5=False)
    tag_flac(path, {"title": "Kiss"})

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        before = _stored(repos, path)
        assert before.work_id is None

        tag_flac(path, {"artist": "Prince"})
        _full_scan(conn, tmp_path)

        after = _stored(repos, path)
        work_exists = _work_exists(conn, after.work_id)

    assert after.id == before.id
    assert after.work_id is not None and work_exists


def _move_keeping_stat(old: Path, new: Path) -> None:
    """A plain move: same size, same mtime, so a rescan adopts the row by its stat."""
    st = old.stat()
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(old, new)
    os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_rescan_leaves_the_work_of_a_row_adopted_by_its_stat(
    migrated_db: str, tmp_path: Path
) -> None:
    """A moved file adopts its row; the fresh read's id must not be grouped.

    The row's work was retitled after it was grouped (as enrichment does with a
    MusicBrainz title), so the file's own tags no longer fuzzy-match any work.
    """
    old = _unfingerprinted_flac(tmp_path / "unsorted" / "kiss.flac", "Kiss", 300)
    new = tmp_path / "Prince" / "kiss.flac"

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        before = _stored(repos, old)
        conn.execute(
            "UPDATE works SET title = 'Sometimes It Snows in April' WHERE id = %s",
            (before.work_id,),
        )
        conn.commit()

        # Grouped in the same chunk as the moved file, before it.
        added = _unfingerprinted_flac(tmp_path / "A New" / "alphabet.flac", "Alphabet St.", 700)
        _move_keeping_stat(old, new)
        _full_scan(conn, tmp_path)

        after = _stored(repos, new)
        added_after = _stored(repos, added)
        added_work_exists = _work_exists(conn, added_after.work_id)

    assert (after.id, after.work_id) == (before.id, before.work_id)
    assert added_after.work_id is not None and added_work_exists
