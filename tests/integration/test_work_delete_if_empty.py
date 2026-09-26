"""Integration: PgWorkRepository.delete_if_empty only removes an unreferenced work.

Enrichment calls it on the local work a file just left for a MusicBrainz work.
``matches`` (work_id and target_id) and ``format_overrides`` also point at
works, so a work they still reference must survive rather than trip the FK.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.broadcast import BroadcastArtist
from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration


def _file(repos: RepositoryFactory, tmp_path: Path, work_id: str | None) -> LibraryFile:
    return repos.library_files.upsert(LibraryFile(
        id=uuid4(),
        file_path=str(tmp_path / f"{uuid4()}.mp3"),
        file_hash=str(uuid4()),
        format="mp3",
        work_id=work_id,
        audio=AudioMetadata(artist_name="Metallica", track_title="Battery"),
    ))


def _match(
    conn: psycopg.Connection[DictRow], repos: RepositoryFactory, file_id: UUID,
    *, work_id: str | None = None, target_id: str | None = None,
) -> None:
    artist = repos.broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="Metallica", normalized_name=str(uuid4())),
    )
    conn.execute(
        """INSERT INTO matches (artist_id, library_file_id, work_id, target_id, target_type)
           VALUES (%s, %s, %s, %s, %s)""",
        (artist.id, file_id, work_id, target_id,
         TargetType.WORK.value if target_id else None),
    )


def _setup(conn: psycopg.Connection[DictRow]) -> tuple[RepositoryFactory, str, str]:
    repos = RepositoryFactory(conn)
    artist_id = repos.artists.upsert_local_artist("Metallica", "metallica")
    return repos, repos.works.create_local("Battery", artist_id), artist_id


def test_deletes_unreferenced_work_and_its_song_master(
    migrated_db: str, tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, work, artist_id = _setup(conn)
        other = repos.works.create_local("Orion", artist_id)
        elsewhere = _file(repos, tmp_path, other)
        repos.song_masters.upsert(SongMaster(
            id=uuid4(), work_id=work, preferred_file_id=elsewhere.id,
            selection_method=SelectionMethod.AUTO,
        ))

        assert repos.works.delete_if_empty(work) is True

        assert repos.works.get_by_id(work) is None
        assert repos.song_masters.get_by_work(work) is None


def test_keeps_work_with_a_file(migrated_db: str, tmp_path: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, work, _ = _setup(conn)
        _file(repos, tmp_path, work)

        assert repos.works.delete_if_empty(work) is False
        assert repos.works.get_by_id(work) is not None


def test_keeps_work_a_match_points_at(migrated_db: str, tmp_path: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, by_work_id, artist_id = _setup(conn)
        by_target = repos.works.create_local("Orion", artist_id)
        moved = _file(repos, tmp_path, None)
        _match(conn, repos, moved.id, work_id=by_work_id)
        _match(conn, repos, moved.id, target_id=by_target)

        assert repos.works.delete_if_empty(by_work_id) is False
        assert repos.works.delete_if_empty(by_target) is False
        assert repos.works.get_by_id(by_work_id) is not None
        assert repos.works.get_by_id(by_target) is not None


def test_keeps_work_with_a_format_override(migrated_db: str, tmp_path: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos, work, _ = _setup(conn)
        moved = _file(repos, tmp_path, None)
        conn.execute(
            """INSERT INTO format_overrides (work_id, format_name, preferred_file_id)
               VALUES (%s, %s, %s)""",
            (work, "classic", moved.id),
        )

        assert repos.works.delete_if_empty(work) is False
        assert repos.works.get_by_id(work) is not None
