"""Integration: after enrichment moves a file to its MusicBrainz work, no master is stranded.

Enrichment moves a file off the local work grouping gave it onto the work MusicBrainz says
its recording performs (`library_enrichment_service._move_file_to_work`). The work the file
left must then follow the rules #108 set for a cross-work fold (spec
2026-09-26-missing-file-reconciliation-design.md, A1 "Applying" step 2, and A2):

- it re-picks its master from its own present files; a master that now points outside the
  work is replaced even if it was manual, and the replacement is AUTO (ruling R1);
- with no present file left, a master pointing outside the work is removed (ruling R2);
- an emptied work is deleted, taking its master with it (existing behaviour);
- a manual master on a file still in the work is kept.

Invariant: no song master points at a file outside its own work.

Every test drives the public entry point (`enrich_by_release`, or `enrich_by_recording`
where noted) against Postgres, with MusicBrainz replaced by `FakeMbClient`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.library_enrichment_service import (
    EnrichmentRepos,
    enrich_by_recording,
    enrich_by_release,
)
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.mb_client import FakeMbClient

pytestmark = pytest.mark.integration

_RELEASE = "00000000-0000-4000-8000-000000000e01"
_RECORDING = "00000000-0000-4000-8000-000000000e02"
_ARTIST = "00000000-0000-4000-8000-000000000e03"
_WORK = "00000000-0000-4000-8000-000000000e04"

_ARTIST_CREDIT = [{"artist": {"id": _ARTIST, "name": "Test Artist", "sort-name": "Artist, T"}}]
_RECORDING_DATA = {
    "id": _RECORDING,
    "title": "Test Track",
    "length": 240_000,
    "artist-credit": _ARTIST_CREDIT,
    "relations": [{"type": "performance", "work": {"id": _WORK, "title": "Test Work"}}],
}
_RELEASE_DATA = {
    "id": _RELEASE,
    "title": "Test Album",
    "artist-credit": _ARTIST_CREDIT,
    "media": [{"tracks": [{"recording": _RECORDING_DATA}]}],
}

EntryPoint = Literal["release", "recording"]

MASTERS_OUTSIDE_THEIR_WORK = """
    SELECT count(*) AS n
    FROM song_masters s
    JOIN library_files f ON f.id = s.preferred_file_id
    WHERE f.work_id IS DISTINCT FROM s.work_id
"""


def _masters_outside_their_work(conn: psycopg.Connection[DictRow]) -> int:
    row = conn.execute(MASTERS_OUTSIDE_THEIR_WORK).fetchone()
    assert row is not None
    return int(row["n"])


def _enrichment_repos(repos: RepositoryFactory) -> EnrichmentRepos:
    """Wired as library_enrichment_tasks wires it."""
    return EnrichmentRepos(
        files=repos.library_files,
        enrichment_queries=repos.library_files,
        recordings=repos.recordings,
        works=repos.works,
        song_masters=repos.song_masters,
        matches=repos.matches,
        artists=repos.artists,
    )


def _enrich(repos: RepositoryFactory, entry: EntryPoint = "release") -> int:
    mb_client = FakeMbClient(
        releases={_RELEASE: _RELEASE_DATA},
        recordings={_RECORDING: _RECORDING_DATA},
    )
    if entry == "release":
        return enrich_by_release(_RELEASE, _enrichment_repos(repos), mb_client)
    return enrich_by_recording(_RECORDING, _enrichment_repos(repos), mb_client)


def _moving_file(
    repos: RepositoryFactory, path: Path, work_id: str, entry: EntryPoint = "release"
) -> LibraryFile:
    """A pending file grouping put on *work_id*; enrichment moves it to the MB work."""
    return repos.library_files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=str(path),
            format="flac",
            work_id=work_id,
            audio=AudioMetadata(
                recording_mbid=_RECORDING,
                release_mbid=_RELEASE if entry == "release" else None,
                duration_ms=240_000,
            ),
        )
    )


def _staying_file(
    repos: RepositoryFactory, path: Path, work_id: str, *, missing: bool = False
) -> LibraryFile:
    """A file with no MusicBrainz tags: neither entry point picks it up, so it stays."""
    lf = repos.library_files.upsert(
        LibraryFile(id=uuid4(), file_path=str(path), format="flac", work_id=work_id)
    )
    if missing:
        repos.library_files.mark_missing(str(path))
    return lf


def _master(repos: RepositoryFactory, work_id: str, file_id: UUID, method: SelectionMethod) -> None:
    repos.song_masters.upsert(
        SongMaster(id=uuid4(), work_id=work_id, preferred_file_id=file_id, selection_method=method)
    )


def _master_of(repos: RepositoryFactory, work_id: str) -> tuple[UUID, SelectionMethod] | None:
    master = repos.song_masters.get_by_work(work_id)
    return None if master is None else (master.preferred_file_id, master.selection_method)


def _mb_work_of(repos: RepositoryFactory, moved: LibraryFile) -> str:
    """The MusicBrainz work the file was moved to (Pg gives it a uuid, not the MBID)."""
    row = repos.library_files.get_by_id(moved.id)
    assert row is not None and row.work_id is not None
    work = repos.works.get_by_id(row.work_id)
    assert work is not None and work.mbid == _WORK
    return row.work_id


def _local_work(repos: RepositoryFactory, title: str = "Test Track") -> str:
    artist_id = repos.artists.upsert_local_artist("Test Artist", "test artist")
    return repos.works.create_local(title, artist_id)


@pytest.mark.parametrize("entry", ["release", "recording"])
def test_old_works_auto_master_moves_to_the_file_it_keeps(
    migrated_db: str, tmp_path: Path, entry: EntryPoint
) -> None:
    """(a) AUTO master on A, A moves, B stays: the old master ends AUTO on B."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        old = _local_work(repos)
        a = _moving_file(repos, tmp_path / "a.flac", old, entry)
        b = _staying_file(repos, tmp_path / "b.flac", old)
        _master(repos, old, a.id, SelectionMethod.AUTO)

        assert _enrich(repos, entry) == 1

        new = _mb_work_of(repos, a)
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, old) == (b.id, SelectionMethod.AUTO)
        assert _master_of(repos, new) == (a.id, SelectionMethod.AUTO)


def test_old_works_manual_master_on_the_moved_file_is_replaced_by_auto(
    migrated_db: str, tmp_path: Path
) -> None:
    """(b) Ruling R1: a MANUAL master on A is invalid once A leaves; it ends AUTO on B."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        old = _local_work(repos)
        a = _moving_file(repos, tmp_path / "a.flac", old)
        b = _staying_file(repos, tmp_path / "b.flac", old)
        _master(repos, old, a.id, SelectionMethod.MANUAL)

        assert _enrich(repos) == 1

        new = _mb_work_of(repos, a)
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, old) == (b.id, SelectionMethod.AUTO)
        assert _master_of(repos, new) == (a.id, SelectionMethod.AUTO)


def test_old_works_valid_manual_master_is_kept(migrated_db: str, tmp_path: Path) -> None:
    """(c) Regression guard: a MANUAL master on B, still in the old work, is kept."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        old = _local_work(repos)
        a = _moving_file(repos, tmp_path / "a.flac", old)
        b = _staying_file(repos, tmp_path / "b.flac", old)
        _master(repos, old, b.id, SelectionMethod.MANUAL)

        assert _enrich(repos) == 1

        new = _mb_work_of(repos, a)
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, old) == (b.id, SelectionMethod.MANUAL)
        assert _master_of(repos, new) == (a.id, SelectionMethod.AUTO)


@pytest.mark.parametrize("old_method", [SelectionMethod.AUTO, SelectionMethod.MANUAL])
def test_old_work_left_with_only_a_missing_file_has_no_master(
    migrated_db: str, tmp_path: Path, old_method: SelectionMethod
) -> None:
    """(d) Ruling R2: only missing M is left, so the old work has no master but still exists.

    Keeping the master on A breaks "never outside the work"; moving it to M breaks A2 "a
    missing file is never chosen as a work's master". M still references the work.
    """
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        old = _local_work(repos)
        a = _moving_file(repos, tmp_path / "a.flac", old)
        _staying_file(repos, tmp_path / "m.flac", old, missing=True)
        _master(repos, old, a.id, old_method)

        assert _enrich(repos) == 1

        new = _mb_work_of(repos, a)
        assert repos.works.get_by_id(old) is not None
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, old) is None
        assert _master_of(repos, new) == (a.id, SelectionMethod.AUTO)


@pytest.mark.parametrize("old_method", [SelectionMethod.AUTO, SelectionMethod.MANUAL])
def test_old_work_left_empty_is_deleted_with_its_master(
    migrated_db: str, tmp_path: Path, old_method: SelectionMethod
) -> None:
    """(e) Regression guard (existing behaviour): the emptied old work and its master go."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        old = _local_work(repos)
        a = _moving_file(repos, tmp_path / "a.flac", old)
        _master(repos, old, a.id, old_method)

        assert _enrich(repos) == 1

        new = _mb_work_of(repos, a)
        assert repos.works.get_by_id(old) is None
        assert _master_of(repos, old) is None
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, new) == (a.id, SelectionMethod.AUTO)


def test_no_master_points_outside_its_work_after_a_mixed_enrichment(
    migrated_db: str, tmp_path: Path
) -> None:
    """(f) The invariant over one enrichment run moving files out of five kinds of old work."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        auto_old, manual_old, valid_manual, only_missing, emptied = (
            _local_work(repos, f"Work {i}") for i in range(5)
        )
        # (a) AUTO on the moving file, a present file stays.
        a1 = _moving_file(repos, tmp_path / "1" / "a.flac", auto_old)
        b1 = _staying_file(repos, tmp_path / "1" / "b.flac", auto_old)
        _master(repos, auto_old, a1.id, SelectionMethod.AUTO)
        # (b) MANUAL on the moving file, a present file stays.
        a2 = _moving_file(repos, tmp_path / "2" / "a.flac", manual_old)
        b2 = _staying_file(repos, tmp_path / "2" / "b.flac", manual_old)
        _master(repos, manual_old, a2.id, SelectionMethod.MANUAL)
        # (c) MANUAL on the file that stays.
        a3 = _moving_file(repos, tmp_path / "3" / "a.flac", valid_manual)
        b3 = _staying_file(repos, tmp_path / "3" / "b.flac", valid_manual)
        _master(repos, valid_manual, b3.id, SelectionMethod.MANUAL)
        # (d) Only a missing file stays.
        a4 = _moving_file(repos, tmp_path / "4" / "a.flac", only_missing)
        _staying_file(repos, tmp_path / "4" / "m.flac", only_missing, missing=True)
        _master(repos, only_missing, a4.id, SelectionMethod.AUTO)
        # (e) Nothing stays.
        a5 = _moving_file(repos, tmp_path / "5" / "a.flac", emptied)
        _master(repos, emptied, a5.id, SelectionMethod.MANUAL)

        assert _enrich(repos) == 5

        new = _mb_work_of(repos, a1)
        moved = {a1.id, a2.id, a3.id, a4.id, a5.id}
        assert all(_mb_work_of(repos, f) == new for f in (a2, a3, a4, a5))
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, auto_old) == (b1.id, SelectionMethod.AUTO)
        assert _master_of(repos, manual_old) == (b2.id, SelectionMethod.AUTO)
        assert _master_of(repos, valid_manual) == (b3.id, SelectionMethod.MANUAL)
        assert repos.works.get_by_id(only_missing) is not None
        assert _master_of(repos, only_missing) is None
        assert repos.works.get_by_id(emptied) is None
        new_master = _master_of(repos, new)
        assert new_master is not None
        assert new_master[0] in moved and new_master[1] == SelectionMethod.AUTO


def test_new_works_valid_manual_master_is_kept(migrated_db: str, tmp_path: Path) -> None:
    """The MB work already has a MANUAL master on its own file X; A arrives; X stays."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Test Artist", "test artist")
        mb_work = repos.works.upsert_from_mb(_WORK, "Test Work", artist_id)
        x = _staying_file(repos, tmp_path / "x.flac", mb_work)
        _master(repos, mb_work, x.id, SelectionMethod.MANUAL)
        old = _local_work(repos)
        a = _moving_file(repos, tmp_path / "a.flac", old)
        b = _staying_file(repos, tmp_path / "b.flac", old)
        _master(repos, old, a.id, SelectionMethod.AUTO)

        assert _enrich(repos) == 1

        assert _mb_work_of(repos, a) == mb_work
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, mb_work) == (x.id, SelectionMethod.MANUAL)
        assert _master_of(repos, old) == (b.id, SelectionMethod.AUTO)


def test_enrichment_leaves_other_works_masters_alone(migrated_db: str, tmp_path: Path) -> None:
    """No file moves in or out of a bystander work, so its AUTO master is not re-picked."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        old = _local_work(repos)
        a = _moving_file(repos, tmp_path / "a.flac", old)
        _staying_file(repos, tmp_path / "b.flac", old)
        _master(repos, old, a.id, SelectionMethod.AUTO)
        bystander = _local_work(repos, "Bystander")
        low = repos.library_files.upsert(
            LibraryFile(
                id=uuid4(), file_path=str(tmp_path / "low.mp3"), format="mp3", work_id=bystander
            )
        )
        _staying_file(repos, tmp_path / "high.flac", bystander)  # scores higher than low
        _master(repos, bystander, low.id, SelectionMethod.AUTO)

        assert _enrich(repos) == 1

        assert _master_of(repos, bystander) == (low.id, SelectionMethod.AUTO)
