"""Integration: PgWorkRepository.merge_into folds duplicate works together.

Every table with a foreign key to ``works`` must be re-pointed before the
sources are deleted — in particular ``matches.work_id`` (migration 0024),
which the older router-level merge never touched.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import WorkMergePlan
from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod, VersionType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.repository_factory import RepositoryFactory
from backend.services.work_dedup_service import merge_work_group

pytestmark = pytest.mark.integration


def _file(
    repos: RepositoryFactory, tmp_path: Path, work_id: str, recording_id: str | None = None,
) -> LibraryFile:
    f = LibraryFile(
        id=uuid4(),
        file_path=str(tmp_path / f"{uuid4()}.mp3"),
        file_hash=str(uuid4()),
        format="mp3",
        work_id=work_id,
        recording_id=recording_id,
        audio=AudioMetadata(artist_name="Alice In Chains", track_title="Would?"),
    )
    return repos.library_files.upsert(f)


def _match(conn: psycopg.Connection[DictRow], repos: RepositoryFactory, file_id: UUID,
           work_id: str) -> UUID:
    artist = repos.broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="AIC", normalized_name=str(uuid4())),
    )
    row = conn.execute(
        """INSERT INTO matches (artist_id, library_file_id, work_id)
           VALUES (%s, %s, %s) RETURNING id""",
        (artist.id, file_id, work_id),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def _override(conn: psycopg.Connection[DictRow], work_id: str, fmt: str, file_id: UUID) -> None:
    conn.execute(
        """INSERT INTO format_overrides (work_id, format_name, preferred_file_id)
           VALUES (%s, %s, %s)""",
        (work_id, fmt, file_id),
    )


def test_merge_into_repoints_every_reference_and_deletes_sources(
    migrated_db: str, tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Alice In Chains", "alice in chains")
        target = repos.works.create_local("Would?", artist_id)
        source = repos.works.create_local("Would?", artist_id)

        target_rec = repos.recordings.get_or_create_local(
            target, VersionType.ORIGINAL.value, "Would?",
        )
        clashing_rec = repos.recordings.get_or_create_local(
            source, VersionType.ORIGINAL.value, "Would?",
        )
        moving_rec = repos.recordings.get_or_create_local(
            source, VersionType.LIVE.value, "Would? (Live)",
        )
        target_file = _file(repos, tmp_path, target, target_rec)
        clash_file = _file(repos, tmp_path, source, clashing_rec)
        live_file = _file(repos, tmp_path, source, moving_rec)
        match_id = _match(conn, repos, clash_file.id, source)
        _override(conn, target, "classic", target_file.id)
        _override(conn, source, "classic", clash_file.id)  # clashes: target wins
        _override(conn, source, "alt", live_file.id)  # moves
        repos.song_masters.upsert(SongMaster(
            id=uuid4(), work_id=source, preferred_file_id=clash_file.id,
            selection_method=SelectionMethod.AUTO,
        ))

        repos.works.merge_into(target, (source,))

        assert repos.works.get_by_id(source) is None
        assert repos.song_masters.get_by_work(source) is None
        assert {f.id for f in repos.library_files.get_by_work(target)} == {
            target_file.id, clash_file.id, live_file.id,
        }
        # The clashing ORIGINAL recording collapsed onto the target's; the
        # LIVE one moved across intact.
        assert repos.recordings.get_by_id(clashing_rec) is None
        moved = repos.library_files.get_by_id(clash_file.id)
        assert moved is not None and moved.recording_id == target_rec
        live = repos.recordings.get_by_id(moving_rec)
        assert live is not None and live.work_id == target
        match = conn.execute(
            "SELECT work_id FROM matches WHERE id = %s", (match_id,),
        ).fetchone()
        assert match is not None and match["work_id"] == target
        overrides = conn.execute(
            """SELECT format_name, preferred_file_id FROM format_overrides
               WHERE work_id = %s ORDER BY format_name""",
            (target,),
        ).fetchall()
        assert [(o["format_name"], o["preferred_file_id"]) for o in overrides] == [
            ("alt", live_file.id), ("classic", target_file.id),
        ]


def test_merge_into_collapses_same_version_recordings_between_sources(
    migrated_db: str, tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Alice In Chains", "alice in chains")
        target = repos.works.create_local("Would?", artist_id)
        first = repos.works.create_local("Would?", artist_id)
        second = repos.works.create_local("Would?", artist_id)
        rec_a = repos.recordings.get_or_create_local(first, VersionType.LIVE.value, "W")
        rec_b = repos.recordings.get_or_create_local(second, VersionType.LIVE.value, "W")
        file_a = _file(repos, tmp_path, first, rec_a)
        file_b = _file(repos, tmp_path, second, rec_b)

        repos.works.merge_into(target, (first, second))

        live = repos.recordings.get_by_work(target)
        assert len(live) == 1
        for f in (file_a, file_b):
            reloaded = repos.library_files.get_by_id(f.id)
            assert reloaded is not None and reloaded.recording_id == live[0].id


def test_list_local_footprints_counts_files_and_matches(
    migrated_db: str, tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Alice In Chains", "alice in chains")
        busy = repos.works.create_local("Would?", artist_id)
        orphan = repos.works.create_local("Would?", artist_id)
        f = _file(repos, tmp_path, busy)
        _match(conn, repos, f.id, busy)

        by_id = {fp.id: fp for fp in repos.works.list_local_footprints()}

        assert (by_id[busy].file_count, by_id[busy].match_count) == (1, 1)
        assert (by_id[orphan].file_count, by_id[orphan].match_count) == (0, 0)


def test_merge_work_group_end_to_end_leaves_a_valid_master(
    migrated_db: str, tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Alice In Chains", "alice in chains")
        orphan = repos.works.create_local("Would?", artist_id)
        real = repos.works.create_local("Would?", artist_id)
        f = _file(repos, tmp_path, real)
        # The "Would?" pattern: the orphan's auto master points at a file
        # that grouping later attached to the other work.
        repos.song_masters.upsert(SongMaster(
            id=uuid4(), work_id=orphan, preferred_file_id=f.id,
            selection_method=SelectionMethod.AUTO,
        ))

        with conn.transaction():
            merge_work_group(
                WorkMergePlan(target_id=orphan, source_ids=(real,)),
                work_repo=repos.works,
                song_master_repo=repos.song_masters,
                library_file_repo=repos.library_files,
            )

        master = repos.song_masters.get_by_work(orphan)
        assert master is not None and master.preferred_file_id == f.id
        assert [x.id for x in repos.library_files.get_by_work(orphan)] == [f.id]
