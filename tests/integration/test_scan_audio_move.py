"""Integration: a FLAC moved and retagged between two scans keeps its row and its matches.

Picard moves and retags in one step, so nothing but the audio is left to tie
the new path to the old row: the retag below also drops the MusicBrainz IDs
and changes album and title, so missing-file reconciliation could not pair
them. The row keeping its id proves the scan adopted it inline.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.services.library_scan_service import scan_folder_incrementally
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_scan_tasks import _run_scan
from tests.fixtures.audio_builders import tag_flac, write_flac

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]

_TAGS = {
    "artist": "Prince",
    "title": "Kiss",
    "album": "Parade",
    "musicbrainz_trackid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "musicbrainz_albumid": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
}


def _kiss(path: Path) -> Path:
    write_flac(path, [(300, -300), (400, -400)])
    tag_flac(path, _TAGS)
    return path


def _move_and_retag(old: Path, new: Path) -> None:
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(old, new)
    tag_flac(
        new,
        {"title": "Kiss (Extended Version)", "album": "Parade (Deluxe)"},
        remove=["musicbrainz_trackid", "musicbrainz_albumid"],
    )


def _full_scan(conn: Conn, root: Path) -> None:
    _run_scan(
        root_path=str(root),
        library_conn=conn,
        repos=RepositoryFactory(conn),
        progress_repo=PgTaskProgressRepository(conn),
        task_id=uuid4().hex,
    )
    conn.commit()


def _seed_match(conn: Conn, file_id: UUID) -> UUID:
    repos = RepositoryFactory(conn)
    artist = repos.broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="PRINCE", normalized_name=str(uuid4()))
    )
    identity = repos.broadcast_identities.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Kiss",
            normalized_title="kiss",
            normalized_signature=str(uuid4()),
        )
    )
    conn.execute(
        "INSERT INTO matches (identity_id, library_file_id) VALUES (%s, %s)",
        (identity.id, file_id),
    )
    return identity.id


def _matched_file(conn: Conn, identity: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT library_file_id FROM matches WHERE identity_id = %s", (identity,)
    ).fetchone()
    return row["library_file_id"] if row else None


def test_full_scan_keeps_the_row_of_a_moved_and_retagged_flac(
    migrated_db: str, tmp_path: Path
) -> None:
    old = _kiss(tmp_path / "unsorted" / "kiss.flac")
    new = tmp_path / "Prince" / "Parade (Deluxe)" / "03 Kiss (Extended Version).flac"

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        before = repos.library_files.get_by_path(str(old))
        assert before is not None and before.audio_hash is not None
        identity = _seed_match(conn, before.id)
        conn.commit()

        _move_and_retag(old, new)
        _full_scan(conn, tmp_path)

        after = repos.library_files.get_by_path(str(new))
        rows = repos.library_files.get_path_statuses_under(str(tmp_path))
        matched = _matched_file(conn, identity)

    assert after is not None
    assert after.id == before.id  # adopted inline; reconciliation keeps the new row's id
    assert after.work_id == before.work_id
    assert after.audio.track_title == "Kiss (Extended Version)"
    assert matched == before.id
    assert len(rows) == 1


def test_watcher_visit_keeps_the_row_of_a_moved_and_retagged_flac(
    migrated_db: str, tmp_path: Path
) -> None:
    old = _kiss(tmp_path / "unsorted" / "kiss.flac")
    new = tmp_path / "Prince" / "03 Kiss.flac"

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        before = repos.library_files.get_by_path(str(old))
        assert before is not None

        _move_and_retag(old, new)
        result = scan_folder_incrementally(
            folder_path=new.parent,
            file_repo=repos.library_files,
            quarantine_repo=repos.library_quarantine,
        )
        after = repos.library_files.get_by_path(str(new))

    assert result.files_relocated == 1
    assert after is not None and after.id == before.id
