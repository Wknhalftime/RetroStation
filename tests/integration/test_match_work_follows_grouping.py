"""Integration: a match takes its file's work, also when grouping gives the file its first one.

A match stores its file's work when it is made (manual resolve reads
``library_files.work_id``), so a file matched before it was grouped has
matches with no work. Grouping later gave the file a work and left the
matches behind (13 such manual matches in the dev DB on 2026-10-13, plus one
left on the work its file moved away from).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.library import LibraryFile
from backend.services.repository_factory import RepositoryFactory
from backend.services.work_move_service import WorkMoveRepos, realign_match_works
from backend.tasks.library_scan_tasks import _run_scan
from tests.fixtures.audio_builders import tag_flac, write_flac

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[dict[str, Any]]


def _full_scan(conn: Conn, root: Path) -> None:
    _run_scan(
        root_path=str(root),
        library_conn=conn,
        repos=RepositoryFactory(conn),
        progress_repo=PgTaskProgressRepository(conn),
        task_id=uuid4().hex,
    )
    conn.commit()


def _identity(repos: RepositoryFactory) -> UUID:
    artist = repos.broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="PRINCE", normalized_name=str(uuid4()))
    )
    identity = repos.broadcast_identities.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="KISS",
            normalized_title="kiss",
            normalized_signature=str(uuid4()),
        )
    )
    return identity.id


def test_grouping_moves_a_match_made_before_the_file_had_a_work(
    migrated_db: str, tmp_path: Path
) -> None:
    path = tmp_path / "kiss.flac"
    write_flac(path, [(300, -300)], store_md5=False)
    tag_flac(path, {"title": "Kiss"})

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        ungrouped = repos.library_files.get_by_path(str(path))
        assert ungrouped is not None and ungrouped.work_id is None
        conn.execute(
            """INSERT INTO matches (identity_id, library_file_id, target_type, work_id,
                                    confidence_score, match_tier)
               VALUES (%s, %s, 'library_file', NULL, 1.0, 'manual')""",
            (_identity(repos), ungrouped.id),
        )
        conn.commit()

        tag_flac(path, {"artist": "Prince"})
        _full_scan(conn, tmp_path)

        grouped = repos.library_files.get_by_path(str(path))
        match = conn.execute(
            "SELECT work_id FROM matches WHERE library_file_id = %s", (ungrouped.id,)
        ).fetchone()

    assert grouped is not None and grouped.work_id is not None
    assert match is not None
    assert match["work_id"] == grouped.work_id


def test_realigning_moves_each_match_to_its_files_work_and_settles_the_work_left(
    migrated_db: str, tmp_path: Path
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Prince", "prince")
        home = repos.works.create_local("Kiss", artist_id)
        left = repos.works.create_local("Kiss (Album Version)", artist_id)
        file = repos.library_files.upsert(
            LibraryFile(
                id=uuid4(), file_path=str(tmp_path / "kiss.flac"), format="flac", work_id=home
            )
        )
        for work_id in (None, left):
            conn.execute(
                """INSERT INTO matches (identity_id, library_file_id, target_type, work_id,
                                        confidence_score, match_tier)
                   VALUES (%s, %s, 'library_file', %s, 1.0, 'manual')""",
                (_identity(repos), file.id, work_id),
            )

        realigned = realign_match_works(
            WorkMoveRepos(
                files=repos.library_files,
                works=repos.works,
                song_masters=repos.song_masters,
                matches=repos.matches,
            )
        )

        works = conn.execute(
            "SELECT work_id FROM matches WHERE library_file_id = %s", (file.id,)
        ).fetchall()
        assert sorted((d.match_work_id or "", d.file_work_id) for d in realigned) == sorted(
            [("", home), (left, home)]
        )
        assert [r["work_id"] for r in works] == [home, home]
        assert repos.matches.list_off_file_work() == []
        assert repos.works.get_by_id(left) is None
        assert repos.works.get_by_id(home) is not None
