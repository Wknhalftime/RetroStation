"""PG: the deletes a missing row needs, and what each leaves alone."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from backend.domain.broadcast import BroadcastArtist
from backend.domain.curation import FormatOverride, SongMaster
from backend.domain.enums import SelectionMethod
from backend.services.repository_factory import RepositoryFactory
from tests.integration.missing_file_seed import identity, library_file, match, work


def _master(repos: RepositoryFactory, work_id: str, file_id: UUID) -> None:
    repos.song_masters.upsert(
        SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=file_id,
            selection_method=SelectionMethod.MANUAL,
            score=0,
        )
    )


def test_delete_missing_deletes_only_a_missing_row(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        gone = library_file(repos, "/m/gone.flac", work(repos), missing=True)

        deleted = repos.library_files.delete_missing(gone.id)
        left = repos.library_files.get_by_id(gone.id)

    assert (deleted, left) == (True, None)


def test_delete_missing_refuses_a_present_row(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        here = library_file(repos, "/m/here.flac", work(repos), missing=False)

        deleted = repos.library_files.delete_missing(here.id)
        left = repos.library_files.get_by_id(here.id)

    assert deleted is False and left is not None


def test_match_delete_for_file_removes_every_match_naming_it(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        work_id = work(repos)
        gone = library_file(repos, "/m/gone.flac", work_id, missing=True)
        other = library_file(repos, "/m/other.flac", work_id, missing=False)
        first, second = identity(repos), identity(repos)
        match(conn, first, gone.id)
        match(conn, second, gone.id)
        match(conn, second, other.id)
        artist = repos.broadcast_artists.upsert(
            BroadcastArtist(id=uuid4(), original_name="PRINCE", normalized_name=str(uuid4()))
        )
        conn.execute(  # an artist-level match naming the file: allowed by the XOR check
            "INSERT INTO matches (artist_id, library_file_id) VALUES (%s, %s)",
            (artist.id, gone.id),
        )

        released = repos.matches.delete_for_file(gone.id)
        left = conn.execute("SELECT identity_id, library_file_id FROM matches").fetchall()

    assert sorted(released) == sorted([first, second])
    assert [(r["identity_id"], r["library_file_id"]) for r in left] == [(second, other.id)]


def test_master_delete_for_file_names_the_works_it_left_without_a_master(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        work_a, work_b, work_c = work(repos, "A"), work(repos, "B"), work(repos, "C")
        gone = library_file(repos, "/m/gone.flac", work_a, missing=True)
        kept = library_file(repos, "/m/kept.flac", work_b, missing=False)
        _master(repos, work_a, gone.id)
        _master(repos, work_b, kept.id)
        _master(repos, work_c, gone.id)  # a master pointing outside its own work

        emptied = repos.song_masters.delete_for_file(gone.id)
        left = [repos.song_masters.get_by_work(w) is not None for w in (work_a, work_b, work_c)]

    assert sorted(emptied) == sorted([work_a, work_c])
    assert left == [False, True, False]


def test_override_delete_for_file_touches_only_that_file(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        work_a, work_b = work(repos, "A"), work(repos, "B")
        gone = library_file(repos, "/m/gone.flac", work_a, missing=True)
        kept = library_file(repos, "/m/kept.flac", work_b, missing=False)
        for work_id, file_id in ((work_a, gone.id), (work_b, kept.id)):
            repos.format_overrides.create(
                FormatOverride(
                    id=uuid4(),
                    work_id=work_id,
                    format_name="classic_rock",
                    preferred_file_id=file_id,
                )
            )

        repos.format_overrides.delete_for_file(gone.id)
        counts = (
            len(repos.format_overrides.list_by_work(work_a)),
            len(repos.format_overrides.list_by_work(work_b)),
        )

    assert counts == (0, 1)
