"""Integration: PgLibraryFileRepository.has_file_beyond_artist_mbid, the Step B pre-check."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.library import AudioMetadata, LibraryFile
from tests.recording_search_reach_cases import ARTIST, CASES, MBID, FileSpec

pytestmark = pytest.mark.integration


def _add(repo: PgLibraryFileRepository, spec: FileSpec) -> None:
    path = rf"D:\m\{uuid4().hex}.flac"
    repo.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            format="flac",
            audio=AudioMetadata(
                recording_mbid=spec.recording_mbid,
                artist_mbid=spec.artist_mbid,
                album_artist_mbid=spec.album_artist_mbid,
                normalized_artist_name=spec.normalized_artist_name,
            ),
        )
    )
    if spec.missing:
        repo.mark_missing(path)


@pytest.mark.parametrize(("specs", "expected"), list(CASES.values()), ids=list(CASES))
def test_has_file_beyond_artist_mbid(
    migrated_db: str, specs: list[FileSpec], expected: bool
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        for spec in specs:
            _add(repo, spec)

        assert repo.has_file_beyond_artist_mbid(ARTIST, MBID) is expected
