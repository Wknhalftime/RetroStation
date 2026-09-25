"""Integration: get_by_folder_path must work on Windows-style paths.

The lookup uses ``LIKE prefix%``. PostgreSQL's default LIKE escape
character is the backslash, so a Windows prefix ending in ``\\`` turned
``\\%`` into a literal percent sign and the query matched nothing. Every
file in a changed folder then looked brand-new to the incremental scan:
it was re-read and re-hashed on every visit, and files that had vanished
from disk were never marked missing.
"""
from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.library import LibraryFile

pytestmark = pytest.mark.integration


def _file(path: str) -> LibraryFile:
    return LibraryFile(id=uuid4(), file_path=path, file_hash="h", format="flac")


@pytest.mark.parametrize(
    ("folder", "direct", "nested", "sibling"),
    [
        (
            r"D:\Media\Music\Albums\10 Years\Minus the Machine",
            r"D:\Media\Music\Albums\10 Years\Minus the Machine\01 - Track.flac",
            r"D:\Media\Music\Albums\10 Years\Minus the Machine\Disc 2\01.flac",
            r"D:\Media\Music\Albums\10 Years\Minus the Machine (Deluxe)\01.flac",
        ),
        (
            "/music/albums/10 Years/Minus the Machine",
            "/music/albums/10 Years/Minus the Machine/01 - Track.flac",
            "/music/albums/10 Years/Minus the Machine/Disc 2/01.flac",
            "/music/albums/10 Years/Minus the Machine (Deluxe)/01.flac",
        ),
    ],
    ids=["windows", "posix"],
)
def test_returns_only_direct_children(
    migrated_db: str, folder: str, direct: str, nested: str, sibling: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        for p in (direct, nested, sibling):
            repo.upsert_write_only(_file(p))
        conn.commit()

        got = {f.file_path for f in repo.get_by_folder_path(folder)}
        assert got == {direct}


def test_like_metacharacters_in_path_are_literal(migrated_db: str) -> None:
    """``_`` and ``%`` are LIKE wildcards; a path containing them must not
    match unrelated rows (``_`` is in a large share of real filenames)."""
    folder = r"D:\Media\Music\Promo Only_ Mainstream Radio, June 1996"
    inside = folder + r"\01 - 100%.flac"
    # Same length as the folder name, with the underscore replaced: a bare
    # ``_`` wildcard would match this too.
    decoy = r"D:\Media\Music\Promo OnlyX Mainstream Radio, June 1996\01.flac"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert_write_only(_file(inside))
        repo.upsert_write_only(_file(decoy))
        conn.commit()

        got = {f.file_path for f in repo.get_by_folder_path(folder)}
        assert got == {inside}
