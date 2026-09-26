"""Pairing each missing row with the present row holding the same track."""

from __future__ import annotations

from uuid import uuid4

from backend.domain.enums import FileStatus
from backend.domain.library import AudioMetadata, LibraryFile, MissingFilePlan
from backend.services.missing_file_reconciliation_service import (
    is_same_track,
    plan_missing_file_moves,
)
from tests.fakes.library_files import FakeLibraryFileRepository


def _row(
    path: str,
    *,
    missing: bool = False,
    work_id: str | None = "w1",
    recording_mbid: str | None = "rec-1",
    release_mbid: str | None = "rel-1",
    duration_ms: int | None = 54_040,
    release_title: str | None = "Pulp Fiction",
    disc_number: int | None = 1,
    track_number: int | None = 16,
    normalized_title: str | None = "ezekiel 25 17",
    normalized_artist_name: str | None = "samuel l jackson",
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        file_hash=None,
        format="flac",
        file_status=FileStatus.MISSING if missing else FileStatus.PRESENT,
        work_id=work_id,
        audio=AudioMetadata(
            recording_mbid=recording_mbid,
            release_mbid=release_mbid,
            duration_ms=duration_ms,
            release_title=release_title,
            disc_number=disc_number,
            track_number=track_number,
            normalized_title=normalized_title,
            normalized_artist_name=normalized_artist_name,
        ),
    )


def _plan(missing: list[LibraryFile], present: list[LibraryFile]) -> MissingFilePlan:
    return plan_missing_file_moves(missing, lambda _m: present)


OLD = r"D:\Music\Pulp Fiction\Samuel L. Jackson - Ezekiel 25;17.flac"
NEW = r"D:\Music\Pulp Fiction_ Music From the Motion Picture\[dialogue] - Ezekiel 25_17.flac"


def test_the_single_present_copy_of_the_recording_is_the_successor() -> None:
    old, new = _row(OLD, missing=True), _row(NEW, duration_ms=54_900)

    plan = _plan([old], [new])

    assert [(m.missing_id, m.successor_id) for m in plan.moves] == [(old.id, new.id)]
    assert plan.ambiguous == () and plan.unmatched == ()


def test_a_retag_without_mbids_matches_on_the_release_track() -> None:
    old = _row(OLD, missing=True, recording_mbid=None, release_mbid=None)
    new = _row(NEW, recording_mbid="rec-9", release_mbid="rel-9")

    assert is_same_track(old, new)


def test_durations_more_than_two_seconds_apart_are_not_the_same_track() -> None:
    old, new = _row(OLD, missing=True), _row(NEW, duration_ms=54_040 + 2_001)

    assert not is_same_track(old, new)


def test_a_row_without_a_duration_is_not_the_same_track() -> None:
    assert not is_same_track(_row(OLD, missing=True, duration_ms=None), _row(NEW))


def test_same_recording_on_another_release_is_not_the_same_track() -> None:
    old = _row(OLD, missing=True)
    new = _row(NEW, release_mbid="rel-2", release_title="Greatest Hits", track_number=4)

    assert not is_same_track(old, new)


def test_no_disc_number_on_both_sides_counts_as_equal() -> None:
    old = _row(OLD, missing=True, recording_mbid=None, disc_number=None)
    new = _row(NEW, recording_mbid=None, disc_number=None)

    assert is_same_track(old, new)


def test_several_copies_are_settled_by_an_identical_file_name() -> None:
    old = _row(r"D:\old\16 Ezekiel.flac", missing=True)
    same_name = _row(r"D:\new\16 Ezekiel.flac")
    other = _row(r"D:\compilation\Ezekiel 25_17.flac")

    plan = _plan([old], [other, same_name])

    assert [m.successor_id for m in plan.moves] == [same_name.id]


def test_several_copies_without_one_name_match_are_ambiguous() -> None:
    old = _row(OLD, missing=True)

    plan = _plan([old], [_row(r"D:\a\x.flac"), _row(r"D:\b\y.flac")])

    assert plan.moves == ()
    assert plan.ambiguous == (OLD,)


def test_a_present_row_succeeds_only_one_missing_row() -> None:
    first = _row(r"D:\a\one.flac", missing=True)
    second = _row(r"D:\b\two.flac", missing=True)
    new = _row(NEW)

    plan = _plan([second, first], [new])

    assert [m.missing_id for m in plan.moves] == [first.id]
    assert plan.ambiguous == (second.file_path,)


def test_a_successor_without_a_work_waits_for_the_next_run() -> None:
    old, new = _row(OLD, missing=True), _row(NEW, work_id=None)

    plan = _plan([old], [new])

    assert plan.moves == ()
    assert plan.unmatched == (OLD,)


def test_a_missing_row_is_never_a_successor() -> None:
    old = _row(OLD, missing=True)
    also_gone = _row(NEW, missing=True)

    plan = _plan([old], [also_gone])

    assert plan.moves == ()
    assert plan.unmatched == (OLD,)


def test_a_move_between_works_is_flagged() -> None:
    old, new = _row(OLD, missing=True, work_id="w1"), _row(NEW, work_id="w2")

    (move,) = _plan([old], [new]).moves

    assert move.crosses_work
    assert (move.missing_work_id, move.successor_work_id) == ("w1", "w2")


def test_candidates_come_from_both_lookups_once_each() -> None:
    from backend.services.missing_file_reconciliation_service import successor_candidates

    repo = FakeLibraryFileRepository()
    old = repo.upsert(_row(OLD))
    repo.mark_missing(OLD)
    new = repo.upsert(_row(NEW))

    assert [c.id for c in successor_candidates(old, repo)] == [new.id]
