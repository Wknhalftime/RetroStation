"""Integration: folding a missing row moves every reference and leaves none dangling."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Recording
from backend.domain.curation import FormatOverride, SongMaster
from backend.domain.enums import FileStatus, SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile, MissingFileMove
from backend.services.missing_file_reconciliation_service import (
    ReconciliationRepos,
    apply_missing_file_move,
    plan_for_library,
)
from backend.services.repository_factory import RepositoryFactory
from backend.tasks import library_scan_tasks
from backend.tasks.library_scan_tasks import apply_missing_file_moves, reconcile_missing_after_scan

pytestmark = pytest.mark.integration


def _repos(repos: RepositoryFactory) -> ReconciliationRepos:
    return ReconciliationRepos(
        files=repos.library_files,
        matches=repos.matches,
        works=repos.works,
        song_masters=repos.song_masters,
        format_overrides=repos.format_overrides,
    )


def _file(repos: RepositoryFactory, path: str, work_id: str, *, missing: bool) -> LibraryFile:
    lf = repos.library_files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            file_hash=None,
            format="flac",
            work_id=work_id,
            audio=AudioMetadata(
                recording_mbid="rec-1",
                release_mbid="rel-1",
                duration_ms=54_040,
                artist_name="Samuel L. Jackson",
                track_title="Ezekiel 25:17",
            ),
        )
    )
    if missing:
        repos.library_files.mark_missing(path)
    return lf


def _identity(repos: RepositoryFactory) -> UUID:
    artist = repos.broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="SAMUEL L JACKSON", normalized_name=str(uuid4()))
    )
    identity = repos.broadcast_identities.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Ezekiel 25:17",
            normalized_title="ezekiel 25 17",
            normalized_signature=str(uuid4()),
        )
    )
    return identity.id


def _match(conn: psycopg.Connection[DictRow], identity: UUID, file_id: UUID, work: str) -> None:
    conn.execute(
        "INSERT INTO matches (identity_id, library_file_id, work_id) VALUES (%s, %s, %s)",
        (identity, file_id, work),
    )


def _matches_of(conn: psycopg.Connection[DictRow], file_id: UUID) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT identity_id, work_id FROM matches WHERE library_file_id = %s ORDER BY identity_id",
        (file_id,),
    ).fetchall()
    return [(str(r["identity_id"]), r["work_id"]) for r in rows]


def test_same_work_fold_moves_matches_master_and_drops_a_duplicate_match(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        work = repos.works.create_local("Ezekiel 25:17", artist_id)
        old = _file(repos, str(tmp_path / "old.flac"), work, missing=True)
        new = _file(repos, str(tmp_path / "new.flac"), work, missing=False)
        shared, moving = _identity(repos), _identity(repos)
        _match(conn, shared, old.id, work)
        _match(conn, shared, new.id, work)
        _match(conn, moving, old.id, work)
        repos.song_masters.upsert(
            SongMaster(
                id=uuid4(),
                work_id=work,
                preferred_file_id=old.id,
                selection_method=SelectionMethod.MANUAL,
            )
        )

        result = reconcile_missing_after_scan(conn, repos)

        assert result is not None and result.reconciled == 1
        assert repos.library_files.get_by_id(old.id) is None
        assert _matches_of(conn, new.id) == sorted([(str(shared), work), (str(moving), work)])
        master = repos.song_masters.get_by_work(work)
        assert master is not None
        assert (master.preferred_file_id, master.selection_method) == (
            new.id,
            SelectionMethod.MANUAL,
        )


def test_cross_work_fold_moves_matches_to_the_new_work_and_deletes_the_old_one(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        old_work = repos.works.create_local("Ezekiel 25 17", artist_id)
        new_work = repos.works.create_local("Ezekiel 25:17", artist_id)
        old = _file(repos, str(tmp_path / "old.flac"), old_work, missing=True)
        new = _file(repos, str(tmp_path / "new.flac"), new_work, missing=False)
        identity = _identity(repos)
        _match(conn, identity, old.id, old_work)
        repos.song_masters.upsert(
            SongMaster(
                id=uuid4(),
                work_id=old_work,
                preferred_file_id=old.id,
                selection_method=SelectionMethod.AUTO,
            )
        )

        reconcile_missing_after_scan(conn, repos)

        assert _matches_of(conn, new.id) == [(str(identity), new_work)]
        assert repos.works.get_by_id(old_work) is None
        assert repos.song_masters.get_by_work(old_work) is None
        dangling = conn.execute(
            """SELECT
                 (SELECT count(*) FROM matches m
                    LEFT JOIN library_files lf ON lf.id = m.library_file_id
                   WHERE m.library_file_id IS NOT NULL AND lf.id IS NULL)
               + (SELECT count(*) FROM song_masters sm
                    LEFT JOIN library_files lf ON lf.id = sm.preferred_file_id
                   WHERE lf.id IS NULL) AS n"""
        ).fetchone()
        assert dangling is not None and dangling["n"] == 0


def test_cross_work_fold_moves_the_format_override_to_the_new_work(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        old_work = repos.works.create_local("Ezekiel 25 17", artist_id)
        new_work = repos.works.create_local("Ezekiel 25:17", artist_id)
        old = _file(repos, str(tmp_path / "old.flac"), old_work, missing=True)
        new = _file(repos, str(tmp_path / "new.flac"), new_work, missing=False)
        repos.format_overrides.create(
            FormatOverride(
                id=uuid4(),
                work_id=old_work,
                format_name="hot_ac",
                preferred_file_id=old.id,
            )
        )

        reconcile_missing_after_scan(conn, repos)

        assert repos.works.get_by_id(old_work) is None
        moved = repos.format_overrides.get(new_work, "hot_ac")
        assert moved is not None
        assert moved.preferred_file_id == new.id


def test_cross_work_fold_keeps_the_new_works_own_override_for_the_same_format(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        old_work = repos.works.create_local("Ezekiel 25 17", artist_id)
        new_work = repos.works.create_local("Ezekiel 25:17", artist_id)
        old = _file(repos, str(tmp_path / "old.flac"), old_work, missing=True)
        new = _file(repos, str(tmp_path / "new.flac"), new_work, missing=False)
        kept_id = uuid4()
        repos.format_overrides.create(
            FormatOverride(
                id=uuid4(),
                work_id=old_work,
                format_name="hot_ac",
                preferred_file_id=old.id,
            )
        )
        repos.format_overrides.create(
            FormatOverride(
                id=kept_id,
                work_id=new_work,
                format_name="hot_ac",
                preferred_file_id=new.id,
            )
        )

        reconcile_missing_after_scan(conn, repos)

        assert repos.works.get_by_id(old_work) is None
        assert repos.format_overrides.get(old_work, "hot_ac") is None
        kept = repos.format_overrides.get(new_work, "hot_ac")
        assert kept is not None
        assert (kept.id, kept.preferred_file_id) == (kept_id, new.id)


def test_cross_work_fold_keeps_an_old_work_that_still_has_a_recording(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        old_work = repos.works.create_local("Ezekiel 25 17", artist_id)
        new_work = repos.works.create_local("Ezekiel 25:17", artist_id)
        repos.recordings.upsert(Recording(id=str(uuid4()), title="Ezekiel", work_id=old_work))
        old = _file(repos, str(tmp_path / "old.flac"), old_work, missing=True)
        new = _file(repos, str(tmp_path / "new.flac"), new_work, missing=False)
        identity = _identity(repos)
        _match(conn, identity, old.id, old_work)

        result = reconcile_missing_after_scan(conn, repos)

        assert result is not None and result.reconciled == 1
        assert _matches_of(conn, new.id) == [(str(identity), new_work)]
        assert repos.library_files.get_by_id(old.id) is None
        assert repos.works.get_by_id(old_work) is not None


def _foldable_pair(
    repos: RepositoryFactory, tmp_path: Path, work: str, name: str, recording: str
) -> tuple[LibraryFile, LibraryFile]:
    def _row(path: Path) -> LibraryFile:
        return repos.library_files.upsert(
            LibraryFile(
                id=uuid4(),
                file_path=str(path),
                file_hash=None,
                format="flac",
                work_id=work,
                audio=AudioMetadata(
                    recording_mbid=recording, release_mbid="rel-1", duration_ms=54_040
                ),
            )
        )

    old = _row(tmp_path / "old" / name)
    repos.library_files.mark_missing(old.file_path)
    return old, _row(tmp_path / "new" / name)


def test_one_failing_fold_does_not_stop_the_others(
    migrated_db: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        work = repos.works.create_local("Ezekiel 25:17", artist_id)
        bad_old, _ = _foldable_pair(repos, tmp_path, work, "a.flac", "rec-a")
        good_old, good_new = _foldable_pair(repos, tmp_path, work, "b.flac", "rec-b")
        conn.commit()

        def _fold_then_fail_one(move: MissingFileMove, r: ReconciliationRepos) -> bool:
            folded = apply_missing_file_move(move, r)
            if move.missing_id == bad_old.id:
                raise psycopg.errors.ForeignKeyViolation("simulated")
            return folded

        monkeypatch.setattr(library_scan_tasks, "apply_missing_file_move", _fold_then_fail_one)

        result = reconcile_missing_after_scan(conn, repos)

        assert result is not None
        assert (result.reconciled, result.failed) == (1, 1)
        assert repos.library_files.get_by_id(good_old.id) is None
        assert repos.library_files.get_by_id(good_new.id) is not None
        kept = repos.library_files.get_by_id(bad_old.id)
        assert kept is not None and kept.file_status == FileStatus.MISSING


def test_a_move_overtaken_since_planning_counts_as_stale(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        work = repos.works.create_local("Ezekiel 25:17", artist_id)
        old, new = _foldable_pair(repos, tmp_path, work, "a.flac", "rec-a")
        plan = plan_for_library(repos.library_files)
        repos.library_files.mark_missing(new.file_path)

        counts = apply_missing_file_moves(plan, conn, _repos(repos))

        assert counts == (0, 0, 1)
        assert repos.library_files.get_by_id(old.id) is not None


def test_reconciliation_repicks_a_master_stranded_on_a_missing_file(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        work = repos.works.create_local("Ezekiel 25:17", artist_id)
        gone = _file(repos, str(tmp_path / "gone.flac"), work, missing=True)
        on_disk = repos.library_files.upsert(
            LibraryFile(
                id=uuid4(),
                file_path=str(tmp_path / "other.flac"),
                file_hash=None,
                format="flac",
                work_id=work,
                audio=AudioMetadata(recording_mbid="rec-other", duration_ms=1_000),
            )
        )
        repos.song_masters.upsert(
            SongMaster(
                id=uuid4(),
                work_id=work,
                preferred_file_id=gone.id,
                selection_method=SelectionMethod.AUTO,
            )
        )

        result = reconcile_missing_after_scan(conn, repos)

        assert result is not None
        assert (result.reconciled, result.unmatched, result.masters_repicked) == (0, 1, 1)
        master = repos.song_masters.get_by_work(work)
        assert master is not None and master.preferred_file_id == on_disk.id


def test_plan_pairs_a_missing_row_with_its_audio_twin_despite_new_tags(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from backend.domain.enums import AudioHashKind
    from backend.domain.library import AudioHash

    audio = AudioHash(AudioHashKind.FLAC_MD5, "f" * 32)
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        work = repos.works.create_local("Ezekiel 25:17", artist_id)
        old = repos.library_files.upsert(
            replace(_file(repos, str(tmp_path / "old.flac"), work, missing=False), audio_hash=audio)
        )
        repos.library_files.mark_missing(old.file_path)
        new = repos.library_files.upsert(
            LibraryFile(
                id=uuid4(),
                file_path=str(tmp_path / "[dialogue] - Ezekiel 25_17.flac"),
                file_hash=None,
                format="flac",
                work_id=work,
                audio_hash=audio,
                audio=AudioMetadata(track_title="[dialogue]", duration_ms=1),
            )
        )

        plan = plan_for_library(repos.library_files)

    assert [(m.missing_id, m.successor_id) for m in plan.moves] == [(old.id, new.id)]
