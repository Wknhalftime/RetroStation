"""Integration (spec C2 remap): a remap is PR A's fold; matches and master follow the file."""

from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.services.missing_file_reconciliation_service import remap_missing_file
from backend.services.repository_factory import RepositoryFactory, reconciliation_repos
from tests.integration.missing_file_seed import identity, library_file, match, work


def test_remap_across_works_moves_matches_and_empties_the_old_work(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        old_work, new_work = work(repos, "Kiss (old tags)"), work(repos, "Kiss")
        gone = library_file(repos, "/m/gone.flac", old_work, missing=True)
        target = library_file(repos, "/m/new.flac", new_work, missing=False)
        matched = identity(repos)
        match(conn, matched, gone.id, old_work)
        repos.song_masters.upsert(
            SongMaster(
                id=uuid4(),
                work_id=old_work,
                preferred_file_id=gone.id,
                selection_method=SelectionMethod.AUTO,
            )
        )

        remap_missing_file(gone.id, target.id, reconciliation_repos(repos))
        row = conn.execute(
            "SELECT library_file_id, work_id FROM matches WHERE identity_id = %s", (matched,)
        ).fetchone()
        gone_left = repos.library_files.get_by_id(gone.id)
        old_work_left = repos.works.get_by_id(old_work)

    assert gone_left is None
    assert row is not None and (row["library_file_id"], row["work_id"]) == (target.id, new_work)
    assert old_work_left is None


def test_remap_within_a_work_keeps_a_manual_master_manual(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        work_id = work(repos)
        gone = library_file(repos, "/m/gone.flac", work_id, missing=True)
        target = library_file(repos, "/m/target.flac", work_id, missing=False)
        repos.song_masters.upsert(
            SongMaster(
                id=uuid4(),
                work_id=work_id,
                preferred_file_id=gone.id,
                selection_method=SelectionMethod.MANUAL,
            )
        )

        remap_missing_file(gone.id, target.id, reconciliation_repos(repos))
        master = repos.song_masters.get_by_work(work_id)

    assert master is not None
    assert (master.preferred_file_id, master.selection_method) == (
        target.id,
        SelectionMethod.MANUAL,
    )
