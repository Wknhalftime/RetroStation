"""Integration: PgSongMasterRepository lists works whose master is a missing file."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration


def _file(repos: RepositoryFactory, path: Path, work_id: str, *, missing: bool) -> LibraryFile:
    lf = repos.library_files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=str(path),
            file_hash=None,
            format="flac",
            work_id=work_id,
            audio=AudioMetadata(artist_name="Metallica", track_title="Battery"),
        )
    )
    if missing:
        repos.library_files.mark_missing(str(path))
    return lf


def _master(
    repos: RepositoryFactory,
    work_id: str,
    file: LibraryFile,
    method: SelectionMethod = SelectionMethod.AUTO,
) -> None:
    repos.song_masters.upsert(
        SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=file.id,
            selection_method=method,
        )
    )


def test_lists_only_works_whose_missing_master_has_a_present_alternative(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Metallica", "metallica")
        stranded = repos.works.create_local("Battery", artist_id)
        only_missing = repos.works.create_local("Orion", artist_id)
        on_disk = repos.works.create_local("One", artist_id)

        _master(repos, stranded, _file(repos, tmp_path / "a.flac", stranded, missing=True))
        _file(repos, tmp_path / "b.flac", stranded, missing=False)
        _master(repos, only_missing, _file(repos, tmp_path / "c.flac", only_missing, missing=True))
        _master(repos, on_disk, _file(repos, tmp_path / "d.flac", on_disk, missing=False))
        _file(repos, tmp_path / "e.flac", on_disk, missing=True)

        assert repos.song_masters.list_work_ids_with_missing_master() == [stranded]


def test_lists_in_work_id_order(migrated_db: str, tmp_path: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Metallica", "metallica")
        works = [repos.works.create_local(f"Track {i}", artist_id) for i in range(3)]
        for i, work in enumerate(works):
            _master(repos, work, _file(repos, tmp_path / f"{i}-old.flac", work, missing=True))
            _file(repos, tmp_path / f"{i}-new.flac", work, missing=False)

        assert repos.song_masters.list_work_ids_with_missing_master() == sorted(works)


def test_leaves_a_manual_master_on_a_missing_file_to_the_user(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Metallica", "metallica")
        work = repos.works.create_local("Battery", artist_id)
        gone = _file(repos, tmp_path / "a.flac", work, missing=True)
        _file(repos, tmp_path / "b.flac", work, missing=False)
        _master(repos, work, gone, SelectionMethod.MANUAL)

        assert repos.song_masters.list_work_ids_with_missing_master() == []
