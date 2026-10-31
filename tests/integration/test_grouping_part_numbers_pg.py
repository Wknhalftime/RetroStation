"""Integration: grouping keeps part numbers apart, through the Postgres candidate query.

"Another Brick in the Wall, Part 1" and "..., Part II" differ by one character and once
scored 96 against a fuzzy threshold of 80, so grouping put every part on one work. Grouping
now compares part numbers; this was the grouping half of the retired part-split repair's
tests (AUD-R048).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.grouping_service import assign_work
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration

_BRICK = "Another Brick in the Wall, "


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
