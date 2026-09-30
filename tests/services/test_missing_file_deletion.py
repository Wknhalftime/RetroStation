"""Deleting missing rows (spec C2): matches go back to review, masters and works follow."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.domain.curation import FormatOverride, SongMaster
from backend.domain.enums import MatchStatus, ReasonCode, SelectionMethod
from backend.domain.library import (
    InvalidMissingFileSelectionError,
    MissingFileDeletion,
    MissingFileSelection,
)
from backend.services.missing_file_reconciliation_service import delete_missing_files
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.services.missing_file_helpers import add_file, add_work, fake_repos, matched_identity


def _only(*ids: UUID) -> MissingFileSelection:
    return MissingFileSelection(ids=ids)


def _master(work_id: str, file_id: UUID, method: SelectionMethod) -> SongMaster:
    return SongMaster(
        id=uuid4(), work_id=work_id, preferred_file_id=file_id, selection_method=method
    )


def test_deleting_a_missing_row_sends_its_identities_back_to_review() -> None:
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    identity = matched_identity(repos, identities, gone.id)

    result = delete_missing_files(_only(gone.id), repos, identities)

    after = identities.get_by_id(identity)
    assert result == MissingFileDeletion(deleted=1, matches_released=1, skipped=0)
    assert repos.files.get_by_id(gone.id) is None
    assert after is not None
    assert (after.match_status, after.match_tier, after.reason_code) == (
        MatchStatus.NEEDS_REVIEW,
        None,
        ReasonCode.LIBRARY_FILE_REMOVED,
    )


def test_an_identity_still_matched_elsewhere_stays_matched() -> None:
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    here = add_file(repos, "/m/here.flac", missing=False)
    identity = matched_identity(repos, identities, gone.id, here.id)

    result = delete_missing_files(_only(gone.id), repos, identities)

    after = identities.get_by_id(identity)
    assert result.matches_released == 1
    assert after is not None and after.match_status == MatchStatus.AUTO_MATCHED
    assert repos.matches.get_by_identity(identity) is not None


def test_rows_no_longer_missing_and_unknown_ids_are_skipped() -> None:
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    here = add_file(repos, "/m/here.flac", missing=False)

    result = delete_missing_files(_only(here.id, uuid4()), repos, identities)

    assert result == MissingFileDeletion(deleted=0, matches_released=0, skipped=2)
    assert repos.files.get_by_id(here.id) is not None


def test_every_row_deletes_all_missing_rows_and_nothing_else() -> None:
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    gone = [add_file(repos, f"/m/{n}.flac", missing=True) for n in ("a", "b")]
    here = add_file(repos, "/m/here.flac", missing=False)

    result = delete_missing_files(MissingFileSelection(every_row=True), repos, identities)

    assert result.deleted == 2
    assert all(repos.files.get_by_id(g.id) is None for g in gone)
    assert repos.files.get_by_id(here.id) is not None


def test_a_master_on_the_deleted_row_moves_to_a_present_file_of_the_work() -> None:
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    here = add_file(repos, "/m/here.flac", missing=False)
    repos.song_masters.upsert(_master("w1", gone.id, SelectionMethod.MANUAL))

    delete_missing_files(_only(gone.id), repos, identities)

    master = repos.song_masters.get_by_work("w1")
    assert master is not None and master.preferred_file_id == here.id


def test_a_master_on_another_file_is_left_alone() -> None:
    # A re-pick would choose the FLAC (format bonus 10 > 3); the MP3 pick must stand.
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    low = add_file(repos, "/m/low.mp3", missing=False, file_format="mp3")
    add_file(repos, "/m/high.flac", missing=False)
    repos.song_masters.upsert(_master("w1", low.id, SelectionMethod.AUTO))

    delete_missing_files(_only(gone.id), repos, identities)

    master = repos.song_masters.get_by_work("w1")
    assert master is not None and master.preferred_file_id == low.id


def test_delete_drops_a_master_nothing_can_replace_then_the_empty_work() -> None:
    repos, identities = fake_repos(), FakeBroadcastTrackIdentityRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    repos.song_masters.upsert(_master("w1", gone.id, SelectionMethod.MANUAL))
    repos.format_overrides.create(
        FormatOverride(id=uuid4(), work_id="w1", format_name="rock", preferred_file_id=gone.id)
    )

    delete_missing_files(_only(gone.id), repos, identities)

    assert repos.song_masters.get_by_work("w1") is None
    assert repos.format_overrides.list_by_work("w1") == []
    assert repos.works.get_by_id("w1") is None


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"ids": (uuid4(),), "every_row": True}],
    ids=["neither", "both"],
)
def test_a_selection_is_either_ids_or_every_row(kwargs: dict[str, object]) -> None:
    with pytest.raises(InvalidMissingFileSelectionError):
        MissingFileSelection(**kwargs)  # type: ignore[arg-type]
