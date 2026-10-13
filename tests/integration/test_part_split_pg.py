"""Integration: splitting mis-grouped part numbers out of a work, against Postgres.

The reported library state: "Another Brick in the Wall, Part 1", "Part 2" and
"Part 3" all on the local work "..., Part II". After the split, Parts 1 and 3
are on works of their own, Part 2 stays, and no song master points outside its
work.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.grouping_service import assign_work
from backend.services.part_split_service import (
    PartSplitRepos,
    plan_part_splits,
    split_misplaced_parts,
)
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration

_BRICK = "Another Brick in the Wall, "

_MASTERS_OUTSIDE_THEIR_WORK = """
    SELECT count(*) AS n
    FROM song_masters s
    JOIN library_files f ON f.id = s.preferred_file_id
    WHERE f.work_id IS DISTINCT FROM s.work_id
"""


def _split_repos(repos: RepositoryFactory) -> PartSplitRepos:
    """Wired as scripts/split_part_works.py wires it."""
    return PartSplitRepos(
        artists=repos.artists,
        works=repos.works,
        files=repos.library_files,
        song_masters=repos.song_masters,
        matches=repos.matches,
    )


def test_split_puts_each_part_on_its_own_work(migrated_db: str, tmp_path: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Pink Floyd", "pink floyd")
        part_ii_work = repos.works.create_local(_BRICK + "Part II", artist_id)
        files = {
            part: repos.library_files.upsert(
                LibraryFile(
                    id=uuid4(),
                    file_path=str(tmp_path / f"{part}.flac"),
                    format="flac",
                    work_id=part_ii_work,
                    audio=AudioMetadata(artist_name="Pink Floyd", track_title=_BRICK + part),
                )
            )
            for part in ("Part II", "Part 1", "Part 2", "Part 3")
        }

        for plan in plan_part_splits(repos.works, repos.library_files):
            split_misplaced_parts(plan, _split_repos(repos))

        def work_title(part: str) -> str:
            row = repos.library_files.get_by_id(files[part].id)
            assert row is not None and row.work_id is not None
            work = repos.works.get_by_id(row.work_id)
            assert work is not None
            return work.title

        assert work_title("Part 1") == _BRICK + "Part 1"
        assert work_title("Part 3") == _BRICK + "Part 3"
        assert work_title("Part 2") == _BRICK + "Part II"
        assert work_title("Part II") == _BRICK + "Part II"
        assert plan_part_splits(repos.works, repos.library_files) == []
        row = conn.execute(_MASTERS_OUTSIDE_THEIR_WORK).fetchone()
        assert row is not None and row["n"] == 0


def test_grouping_against_postgres_keeps_parts_apart(migrated_db: str, tmp_path: Path) -> None:
    """The fixed fuzzy match, through the Pg candidate query, in the reported order."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        work_ids = []
        for part in ("Part II", "Part 1", "Part 2", "Part 3"):
            lf = repos.library_files.upsert(
                LibraryFile(
                    id=uuid4(),
                    file_path=str(tmp_path / f"{part}.flac"),
                    format="flac",
                    audio=AudioMetadata(artist_name="Pink Floyd", track_title=_BRICK + part),
                )
            )
            result = assign_work(
                lf,
                artist_repo=repos.artists,
                work_repo=repos.works,
                library_file_repo=repos.library_files,
                recording_repo=repos.recordings,
                song_master_repo=repos.song_masters,
            )
            assert result is not None
            repos.library_files.update_work_id(lf.id, result.work_id)
            work_ids.append(result.work_id)

        part_ii, part_1, part_2, part_3 = work_ids
        assert part_2 == part_ii
        assert len({part_ii, part_1, part_3}) == 3
