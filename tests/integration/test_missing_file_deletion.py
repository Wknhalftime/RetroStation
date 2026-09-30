"""Integration (spec C2 delete): the real FKs let the row, its master and its work go, matches
are released, and a manual master on the deleted row gives way to a present file (a Pg upsert
never overwrites a manual pick)."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from backend.domain.curation import FormatOverride, SongMaster
from backend.domain.enums import MatchStatus, SelectionMethod
from backend.domain.library import MissingFileDeletion, MissingFileSelection
from backend.services.missing_file_reconciliation_service import delete_missing_files
from backend.services.repository_factory import RepositoryFactory, reconciliation_repos
from tests.integration.missing_file_seed import identity, library_file, match, work


def _delete(repos: RepositoryFactory, *ids: UUID) -> MissingFileDeletion:
    return delete_missing_files(
        MissingFileSelection(ids=ids), reconciliation_repos(repos), repos.broadcast_identities
    )


def test_deleting_the_only_file_of_a_work_removes_every_reference(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        work_id = work(repos)
        gone = library_file(repos, "/m/gone.flac", work_id, missing=True)
        matched = identity(repos)
        match(conn, matched, gone.id, work_id)
        repos.song_masters.upsert(
            SongMaster(
                id=uuid4(),
                work_id=work_id,
                preferred_file_id=gone.id,
                selection_method=SelectionMethod.MANUAL,
            )
        )
        repos.format_overrides.create(
            FormatOverride(
                id=uuid4(), work_id=work_id, format_name="rock", preferred_file_id=gone.id
            )
        )

        result = _delete(repos, gone.id)
        after = repos.broadcast_identities.get_by_id(matched)
        work_left = repos.works.get_by_id(work_id)

    # No dangling-reference check: the FKs have no ON DELETE and are not deferrable, so a
    # reference left behind makes the delete itself raise.
    assert (result.deleted, result.matches_released) == (1, 1)
    assert after is not None and after.match_status == MatchStatus.NEEDS_REVIEW
    assert work_left is None


def test_a_manual_master_on_the_deleted_row_gives_way_to_a_present_file(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        work_id = work(repos)
        gone = library_file(repos, "/m/gone.flac", work_id, missing=True)
        here = library_file(repos, "/m/here.flac", work_id, missing=False)
        repos.song_masters.upsert(
            SongMaster(
                id=uuid4(),
                work_id=work_id,
                preferred_file_id=gone.id,
                selection_method=SelectionMethod.MANUAL,
            )
        )

        _delete(repos, gone.id)
        master = repos.song_masters.get_by_work(work_id)

    assert master is not None
    assert (master.preferred_file_id, master.selection_method) == (
        here.id,
        SelectionMethod.AUTO,
    )
