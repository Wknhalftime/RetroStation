"""PG: the Missing Files listing: order, paging, work, matches, a present copy, totals."""

from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.domain.broadcast import BroadcastArtist
from backend.domain.library import AudioMetadata
from backend.services.repository_factory import RepositoryFactory
from tests.integration.missing_file_seed import identity, library_file, match, work

_KISS = AudioMetadata(artist_name="Prince", track_title="Kiss", release_title="Parade")


def test_list_page_describes_each_missing_row(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        kept_work, lost_work = work(repos, "Kiss"), work(repos, "Sometimes It Snows")
        b = library_file(repos, "/m/b.flac", kept_work, missing=True, audio=_KISS).id
        library_file(repos, "/m/kept.flac", kept_work, missing=False, audio=_KISS)
        a = library_file(repos, "/m/a.flac", lost_work, missing=True, audio=_KISS).id
        c = library_file(repos, "/m/c.flac", None, missing=True, audio=_KISS).id
        match(conn, identity(repos), b)
        match(conn, identity(repos), b)
        match(conn, identity(repos), c)

        page = repos.missing_files.list_page(0, 10)

    assert [r.id for r in page.rows] == [a, b, c]
    by_id = {r.id: r for r in page.rows}
    assert (by_id[b].work_title, by_id[b].match_count, by_id[b].work_has_present_file) == (
        "Kiss",
        2,
        True,
    )
    assert (by_id[a].work_title, by_id[a].match_count, by_id[a].work_has_present_file) == (
        "Sometimes It Snows",
        0,
        False,
    )
    assert (by_id[c].work_id, by_id[c].work_has_present_file) == (None, False)
    assert by_id[a].missing_since is not None
    assert (by_id[a].artist_name, by_id[a].track_title, by_id[a].release_title) == (
        "Prince",
        "Kiss",
        "Parade",
    )
    assert (page.total, page.total_match_count) == (3, 3)


def test_list_page_pages_but_totals_cover_every_missing_row(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        for name in ("a", "b", "c", "d"):
            library_file(repos, f"/m/{name}.flac", None, missing=True)
        library_file(repos, "/m/present.flac", None, missing=False)

        page = repos.missing_files.list_page(1, 2)

    assert [r.file_path for r in page.rows] == ["/m/b.flac", "/m/c.flac"]
    assert page.total == 4


def test_match_count_counts_identity_matches_only(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        gone = library_file(repos, "/m/gone.flac", work(repos), missing=True).id
        match(conn, identity(repos), gone)
        artist = repos.broadcast_artists.upsert(
            BroadcastArtist(id=uuid4(), original_name="PRINCE", normalized_name=str(uuid4()))
        )
        conn.execute(  # an artist-level match naming the row: a delete releases nothing (R10)
            "INSERT INTO matches (artist_id, library_file_id) VALUES (%s, %s)",
            (artist.id, gone),
        )

        page = repos.missing_files.list_page(0, 10)

    assert [r.match_count for r in page.rows] == [1]
    assert page.total_match_count == 1
