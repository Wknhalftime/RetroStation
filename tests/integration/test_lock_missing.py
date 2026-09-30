"""Integration: PgLibraryFileRepository.lock_missing locks a row only while it is MISSING.

The row lock itself is not tested here (no concurrency test); these cases pin the
status check a deletion relies on.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.services.repository_factory import RepositoryFactory
from tests.integration.missing_file_seed import library_file

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("missing", "locked"), [(True, True), (False, False)])
def test_only_a_missing_row_is_locked(migrated_db: str, missing: bool, locked: bool) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        row = library_file(repos, "/m/row.flac", None, missing=missing)

        assert repos.library_files.lock_missing(row.id) is locked


def test_an_unknown_id_is_not_locked(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        assert RepositoryFactory(conn).library_files.lock_missing(uuid4()) is False
