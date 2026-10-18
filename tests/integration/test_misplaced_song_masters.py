"""Integration: a work's song master never points at a file on another work.

A case-duplicate repair folds a stale row into its keeper. When grouping put
the keeper on another work, the stale row's master was re-pointed at the
keeper and its work was left holding nothing but that master (14 such
masters in the dev DB on 2026-10-13).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import AudioMetadata, CaseDuplicateRepair, LibraryFile
from backend.services.case_duplicate_service import apply_case_duplicate_repair
from backend.services.repository_factory import RepositoryFactory, reconciliation_repos
from backend.services.work_move_service import WorkMoveRepos, settle_misplaced_masters

pytestmark = pytest.mark.integration


def _file(repos: RepositoryFactory, path: Path, work_id: str) -> LibraryFile:
    return repos.library_files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=str(path),
            format="flac",
            work_id=work_id,
            audio=AudioMetadata(artist_name="Bon Jovi", track_title="Price of Love"),
        )
    )


def _auto_master(repos: RepositoryFactory, work_id: str, file: LibraryFile) -> None:
    repos.song_masters.upsert(
        SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=file.id,
            selection_method=SelectionMethod.AUTO,
        )
    )


def _misplaced(conn: psycopg.Connection[DictRow]) -> list[str]:
    rows = conn.execute(
        """SELECT s.work_id FROM song_masters s
           JOIN library_files f ON f.id = s.preferred_file_id
           WHERE f.work_id IS DISTINCT FROM s.work_id
           ORDER BY s.work_id"""
    ).fetchall()
    return [r["work_id"] for r in rows]


def test_case_duplicate_fold_across_works_leaves_no_master_off_its_work(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Bon Jovi", "bon jovi")
        stale_work = repos.works.create_local("Price of Love", artist_id)
        keeper_work = repos.works.create_local("The Price of Love", artist_id)
        stale = _file(repos, tmp_path / "7800 Fahrenheit" / "Price of Love.flac", stale_work)
        keeper = _file(repos, tmp_path / "7800 fahrenheit" / "Price of Love.flac", keeper_work)
        _auto_master(repos, stale_work, stale)
        _auto_master(repos, keeper_work, keeper)

        apply_case_duplicate_repair(
            CaseDuplicateRepair(
                keeper_id=keeper.id,
                keeper_path=keeper.file_path,
                stale_ids=(stale.id,),
            ),
            reconciliation_repos(repos),
        )

        assert _misplaced(conn) == []
        assert repos.works.get_by_id(stale_work) is None
        master = repos.song_masters.get_by_work(keeper_work)
        assert master is not None
        assert master.preferred_file_id == keeper.id


def _work_move_repos(repos: RepositoryFactory) -> WorkMoveRepos:
    return WorkMoveRepos(
        files=repos.library_files,
        works=repos.works,
        song_masters=repos.song_masters,
        matches=repos.matches,
    )


def test_listing_names_only_works_whose_master_is_off_the_work(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Bon Jovi", "bon jovi")
        home = repos.works.create_local("Tokyo Road", artist_id)
        left = repos.works.create_local("Tokyo Road (Live)", artist_id)
        file = _file(repos, tmp_path / "Tokyo Road.flac", home)
        _auto_master(repos, home, file)
        _auto_master(repos, left, file)

        assert repos.song_masters.list_work_ids_with_master_off_work() == [left]


def test_settling_misplaced_masters_deletes_repicks_or_drops_each(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Bon Jovi", "bon jovi")
        home = repos.works.create_local("Silent Night", artist_id)
        moved = _file(repos, tmp_path / "Silent Night.flac", home)
        _auto_master(repos, home, moved)
        # Nothing but the master references it: the work goes.
        emptied = repos.works.create_local("Silent Night (Album Version)", artist_id)
        _auto_master(repos, emptied, moved)
        # It still has a file of its own: the master is re-picked to it.
        kept = repos.works.create_local("Silent Night!", artist_id)
        own = _file(repos, tmp_path / "Silent Night!.flac", kept)
        _auto_master(repos, kept, moved)
        # A format override still references it: the work stays, without a master.
        held = repos.works.create_local("Silent Night?", artist_id)
        _auto_master(repos, held, moved)
        conn.execute(
            """INSERT INTO format_overrides (work_id, format_name, preferred_file_id)
               VALUES (%s, 'hot_ac', %s)""",
            (held, moved.id),
        )

        settled = settle_misplaced_masters(_work_move_repos(repos))

        assert sorted(settled) == sorted([emptied, kept, held])
        assert _misplaced(conn) == []
        assert repos.works.get_by_id(emptied) is None
        kept_master = repos.song_masters.get_by_work(kept)
        assert kept_master is not None
        assert kept_master.preferred_file_id == own.id
        assert repos.works.get_by_id(held) is not None
        assert repos.song_masters.get_by_work(held) is None
        home_master = repos.song_masters.get_by_work(home)
        assert home_master is not None
        assert home_master.preferred_file_id == moved.id
