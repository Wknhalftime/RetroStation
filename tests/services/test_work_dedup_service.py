"""Unit tests for planning and merging duplicate local works."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.domain.catalog import WorkFootprint, WorkMergePlan
from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.work_dedup_service import (
    MergeTargetNotFoundError,
    merge_work_group,
    plan_duplicate_merges,
)
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository


def _fp(
    work_id: str, title: str, artist: str = "a1", files: int = 0, matches: int = 0,
) -> WorkFootprint:
    return WorkFootprint(
        id=work_id, title=title, artist_id=artist, file_count=files, match_count=matches,
    )


# ---------------------------------------------------------------------------
# plan_duplicate_merges
# ---------------------------------------------------------------------------


def test_distinct_titles_produce_no_plans() -> None:
    assert plan_duplicate_merges([_fp("w1", "Would?"), _fp("w2", "Rooster")]) == []


def test_same_title_same_artist_is_one_group() -> None:
    plans = plan_duplicate_merges([_fp("w1", "Would?"), _fp("w2", "Would?", files=1)])
    assert plans == [WorkMergePlan(target_id="w2", source_ids=("w1",))]


def test_same_title_different_artist_is_not_a_duplicate() -> None:
    plans = plan_duplicate_merges([_fp("w1", "Angel", "a1"), _fp("w2", "Angel", "a2")])
    assert plans == []


def test_titles_equal_after_normalization_group_together() -> None:
    plans = plan_duplicate_merges([
        _fp("w1", "You Give Love a Bad Name"),
        _fp("w2", "You Give Love A Bad Name"),
    ])
    assert len(plans) == 1


def test_title_that_normalizes_to_nothing_never_groups() -> None:
    assert plan_duplicate_merges([_fp("w1", "!!!"), _fp("w2", "???")]) == []


def test_majority_spelling_survives_over_a_stray_bracket() -> None:
    plans = plan_duplicate_merges([
        _fp("w1", "Walk This Way)", files=1),
        _fp("w2", "Walk This Way", files=1),
        _fp("w3", "Walk This Way", files=1),
    ])
    assert plans[0].target_id in {"w2", "w3"}


def test_decode_scarred_title_never_survives() -> None:
    plans = plan_duplicate_merges([
        _fp("w1", "This Ain�t a Love Song", files=5),
        _fp("w2", "This Ain't a Love Song"),
    ])
    assert plans[0].target_id == "w2"


def test_more_files_breaks_a_spelling_tie() -> None:
    plans = plan_duplicate_merges([_fp("w1", "Would?"), _fp("w2", "Would?", files=2)])
    assert plans[0].target_id == "w2"


def test_more_matches_breaks_a_file_tie() -> None:
    plans = plan_duplicate_merges([
        _fp("w1", "Would?", files=1),
        _fp("w2", "Would?", files=1, matches=3),
    ])
    assert plans[0].target_id == "w2"


def test_smallest_id_breaks_a_full_tie() -> None:
    plans = plan_duplicate_merges([_fp("w2", "Would?"), _fp("w1", "Would?")])
    assert plans == [WorkMergePlan(target_id="w1", source_ids=("w2",))]


def test_group_of_three_lists_every_other_member_as_a_source() -> None:
    plans = plan_duplicate_merges([
        _fp("w3", "Would?"), _fp("w1", "Would?", files=1), _fp("w2", "Would?"),
    ])
    assert plans == [WorkMergePlan(target_id="w1", source_ids=("w2", "w3"))]


# ---------------------------------------------------------------------------
# merge_work_group
# ---------------------------------------------------------------------------


def _repos() -> tuple[FakeWorkRepository, FakeSongMasterRepository, FakeLibraryFileRepository]:
    works = FakeWorkRepository()
    files = FakeLibraryFileRepository()
    works.set_library_file_repo(files)
    return works, FakeSongMasterRepository(), files


def _file_in(files: FakeLibraryFileRepository, work_id: str, fmt: str = "mp3") -> LibraryFile:
    f = LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4()}.{fmt}",
        file_hash=str(uuid4()),
        format=fmt,
        work_id=work_id,
        audio=AudioMetadata(artist_name="Alice In Chains", track_title="Would?"),
    )
    files.upsert(f)
    return f


def _master(work_id: str, file_id: UUID, method: SelectionMethod) -> SongMaster:
    return SongMaster(
        id=uuid4(), work_id=work_id, preferred_file_id=file_id, selection_method=method,
    )


def _merge(
    plan: WorkMergePlan,
    works: FakeWorkRepository,
    masters: FakeSongMasterRepository,
    files: FakeLibraryFileRepository,
) -> None:
    merge_work_group(
        plan, work_repo=works, song_master_repo=masters, library_file_repo=files,
    )


def test_merge_moves_files_and_deletes_sources() -> None:
    works, masters, files = _repos()
    target = works.create_local("Would?", "a1")
    source = works.create_local("Would?", "a1")
    moved = _file_in(files, source)

    _merge(WorkMergePlan(target, (source,)), works, masters, files)

    assert works.get_by_id(source) is None
    assert [f.id for f in files.get_by_work(target)] == [moved.id]


def test_merge_points_an_orphan_targets_master_at_a_real_file() -> None:
    works, masters, files = _repos()
    target = works.create_local("Would?", "a1")
    source = works.create_local("Would?", "a1")
    elsewhere = _file_in(files, "some-other-work")
    masters.upsert(_master(target, elsewhere.id, SelectionMethod.AUTO))
    moved = _file_in(files, source)

    _merge(WorkMergePlan(target, (source,)), works, masters, files)

    master = masters.get_by_work(target)
    assert master is not None
    assert master.preferred_file_id == moved.id
    assert master.selection_method == SelectionMethod.AUTO


def test_merge_picks_the_best_scoring_file_as_master() -> None:
    works, masters, files = _repos()
    target = works.create_local("Would?", "a1")
    source = works.create_local("Would?", "a1")
    _file_in(files, target, fmt="mp3")
    flac = _file_in(files, source, fmt="flac")

    _merge(WorkMergePlan(target, (source,)), works, masters, files)

    master = masters.get_by_work(target)
    assert master is not None
    assert master.preferred_file_id == flac.id


def test_merge_carries_a_sources_manual_choice_to_the_target() -> None:
    works, masters, files = _repos()
    target = works.create_local("Would?", "a1")
    source = works.create_local("Would?", "a1")
    _file_in(files, target, fmt="flac")
    chosen = _file_in(files, source, fmt="mp3")
    masters.upsert(_master(source, chosen.id, SelectionMethod.MANUAL))

    _merge(WorkMergePlan(target, (source,)), works, masters, files)

    master = masters.get_by_work(target)
    assert master is not None
    assert master.preferred_file_id == chosen.id
    assert master.selection_method == SelectionMethod.MANUAL


def test_merge_keeps_the_targets_own_manual_choice() -> None:
    works, masters, files = _repos()
    target = works.create_local("Would?", "a1")
    source = works.create_local("Would?", "a1")
    own = _file_in(files, target, fmt="mp3")
    masters.upsert(_master(target, own.id, SelectionMethod.MANUAL))
    other = _file_in(files, source, fmt="flac")
    masters.upsert(_master(source, other.id, SelectionMethod.MANUAL))

    _merge(WorkMergePlan(target, (source,)), works, masters, files)

    master = masters.get_by_work(target)
    assert master is not None
    assert master.preferred_file_id == own.id


def test_merge_into_a_vanished_target_raises() -> None:
    works, masters, files = _repos()
    source = works.create_local("Would?", "a1")
    with pytest.raises(MergeTargetNotFoundError):
        _merge(WorkMergePlan("gone", (source,)), works, masters, files)
    assert works.get_by_id(source) is not None
