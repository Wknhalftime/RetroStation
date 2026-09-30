"""Integration: PgLibraryFileRepository.lock_fold_pair allows a fold only from MISSING to PRESENT.

The row locks themselves are not tested here (no concurrency test); these cases
pin the status check the fold relies on.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration


def _file(repos: RepositoryFactory, path: str, *, missing: bool) -> LibraryFile:
    row = repos.library_files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            format="flac",
            audio=AudioMetadata(artist_name="Seal", track_title="Kiss from a Rose"),
        )
    )
    if missing:
        repos.library_files.mark_missing(path)
    return row


def test_a_missing_row_and_a_present_successor_may_fold(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        gone = _file(repos, "/m/gone.flac", missing=True)
        here = _file(repos, "/m/here.flac", missing=False)

        assert repos.library_files.lock_fold_pair(gone.id, here.id) is True


@pytest.mark.parametrize(
    ("source_missing", "target_missing"),
    [(False, False), (True, True), (False, True)],
    ids=["source-present", "target-missing", "both-wrong"],
)
def test_a_pair_in_the_wrong_states_may_not_fold(
    migrated_db: str, source_missing: bool, target_missing: bool
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        source = _file(repos, "/m/source.flac", missing=source_missing)
        target = _file(repos, "/m/target.flac", missing=target_missing)

        assert repos.library_files.lock_fold_pair(source.id, target.id) is False


def test_an_unknown_id_may_not_fold(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        gone = _file(repos, "/m/gone.flac", missing=True)
        here = _file(repos, "/m/here.flac", missing=False)

        assert repos.library_files.lock_fold_pair(uuid4(), here.id) is False
        assert repos.library_files.lock_fold_pair(gone.id, uuid4()) is False
