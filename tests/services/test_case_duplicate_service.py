"""Planning repairs for rows that name one file in different case."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from uuid import uuid4

import pytest

from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import CaseDuplicateRepair, LibraryFile
from backend.services.case_duplicate_service import (
    apply_case_duplicate_repair,
    on_disk_spelling,
    plan_case_duplicate_repairs,
)
from tests.services.missing_file_helpers import add_work, fake_repos


def _row(path: str) -> LibraryFile:
    return LibraryFile(id=uuid4(), file_path=path, format="flac")


def test_keeper_is_the_row_spelled_as_on_disk() -> None:
    stale = _row(r"D:\Seal\Seal - Kiss from a Rose.flac")
    keeper = _row(r"D:\Seal\Seal - Kiss From a Rose.flac")
    older = _row(r"D:\Seal\SEAL - KISS FROM A ROSE.flac")

    repairs, unresolved = plan_case_duplicate_repairs(
        [[stale, keeper, older]],
        spelling=lambda _p: keeper.file_path,
    )

    assert unresolved == []
    assert len(repairs) == 1
    assert repairs[0].keeper_id == keeper.id
    assert set(repairs[0].stale_ids) == {stale.id, older.id}


@pytest.mark.parametrize(
    "disk",
    [None, r"D:\Seal\seal - kiss from a rose.flac"],
    ids=["gone-or-unreadable", "spelled-a-third-way"],
)
def test_group_with_no_row_spelled_as_on_disk_is_left_alone(disk: str | None) -> None:
    group = [
        _row(r"D:\Seal\Seal - Kiss from a Rose.flac"),
        _row(r"D:\Seal\Seal - Kiss From a Rose.flac"),
    ]

    repairs, unresolved = plan_case_duplicate_repairs([group], spelling=lambda _p: disk)

    assert repairs == []
    assert unresolved == [[f.file_path for f in group]]


def test_apply_deletes_stale_rows_and_keeps_the_keeper() -> None:
    repos = fake_repos()
    repo = repos.files
    stale = repo.upsert(_row(r"D:\Bon Jovi\Keep The Faith\a.flac"))
    keeper = repo.upsert(_row(r"D:\Bon Jovi\Keep the Faith\a.flac"))
    repairs, _ = plan_case_duplicate_repairs(
        repo.get_case_duplicate_groups(),
        spelling=lambda _p: keeper.file_path,
    )

    apply_case_duplicate_repair(repairs[0], repos)

    assert repo.get_by_id(stale.id) is None
    assert repo.get_by_id(keeper.id) is not None
    assert repo.get_case_duplicate_groups() == []


def test_apply_across_works_settles_the_work_the_stale_row_left() -> None:
    repos = fake_repos()
    stale_work, keeper_work = add_work(repos, "w-stale"), add_work(repos, "w-keeper")
    stale = repos.files.upsert(
        dataclasses.replace(_row(r"D:\Bon Jovi\7800\a.flac"), work_id=stale_work)
    )
    keeper = repos.files.upsert(
        dataclasses.replace(_row(r"D:\Bon Jovi\7800\A.flac"), work_id=keeper_work)
    )
    repos.song_masters.upsert(
        SongMaster(
            id=uuid4(),
            work_id=stale_work,
            preferred_file_id=stale.id,
            selection_method=SelectionMethod.AUTO,
        )
    )

    apply_case_duplicate_repair(
        CaseDuplicateRepair(
            keeper_id=keeper.id, keeper_path=keeper.file_path, stale_ids=(stale.id,)
        ),
        repos,
    )

    assert repos.works.get_by_id(stale_work) is None
    assert repos.song_masters.get_by_work(stale_work) is None


def test_on_disk_spelling_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert on_disk_spelling(str(tmp_path / "gone.flac")) is None


def test_on_disk_spelling_reads_the_disks_case(tmp_path: Path) -> None:
    real = tmp_path / "Kiss From a Rose.flac"
    real.touch()
    respelled = tmp_path / "Kiss from a Rose.flac"
    if not respelled.exists():
        pytest.skip("only a case-insensitive filesystem opens a file under another case")

    assert on_disk_spelling(str(real)) == str(real)
    assert on_disk_spelling(str(respelled)) == str(real)
