"""Integration: PgMatchRepository.move_to_work re-points a file's matches.

``matches.work_id`` mirrors ``library_files.work_id`` for the matched file;
enrichment calls this when a file leaves grouping's local work for the
MusicBrainz work its recording performs.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.broadcast import BroadcastArtist
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration


def _file(repos: RepositoryFactory, tmp_path: Path, work_id: str) -> LibraryFile:
    return repos.library_files.upsert(LibraryFile(
        id=uuid4(),
        file_path=str(tmp_path / f"{uuid4()}.mp3"),
        file_hash=str(uuid4()),
        format="mp3",
        work_id=work_id,
        audio=AudioMetadata(artist_name="Metallica", track_title="Battery"),
    ))


def _match(
    conn: psycopg.Connection[DictRow], repos: RepositoryFactory, file_id: UUID, work_id: str,
) -> UUID:
    artist = repos.broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="Metallica", normalized_name=str(uuid4())),
    )
    row = conn.execute(
        """INSERT INTO matches (artist_id, library_file_id, work_id)
           VALUES (%s, %s, %s) RETURNING id""",
        (artist.id, file_id, work_id),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def _work_of(conn: psycopg.Connection[DictRow], match_id: UUID) -> str | None:
    row = conn.execute("SELECT work_id FROM matches WHERE id = %s", (match_id,)).fetchone()
    assert row is not None
    return None if row["work_id"] is None else str(row["work_id"])


def test_move_to_work_repoints_only_the_files_matches(migrated_db: str, tmp_path: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Metallica", "metallica")
        local_work = repos.works.create_local("Battery", artist_id)
        mb_work = repos.works.upsert_from_mb(str(uuid4()), "Battery", artist_id)
        moving = _file(repos, tmp_path, local_work)
        staying = _file(repos, tmp_path, local_work)
        first = _match(conn, repos, moving.id, local_work)
        second = _match(conn, repos, moving.id, local_work)
        other = _match(conn, repos, staying.id, local_work)

        repos.matches.move_to_work(moving.id, mb_work)

        assert _work_of(conn, first) == mb_work
        assert _work_of(conn, second) == mb_work
        assert _work_of(conn, other) == local_work
