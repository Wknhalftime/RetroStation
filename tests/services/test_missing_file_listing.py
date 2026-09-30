"""Listing (spec C2): each missing row comes with the present files A1 would accept for it."""

from __future__ import annotations

from backend.domain.library import AudioHash, LibraryFile, MissingFileRow
from backend.services.missing_file_reconciliation_service import (
    choose_successor,
    list_missing_files,
    ready_successors,
    successor_candidates,
)
from tests.fakes.missing_files import FakeMissingFileListingRepository
from tests.services.missing_file_helpers import add_file, add_work, fake_repos

_HASH = AudioHash.parse("flac-md5:" + "a" * 32)


def _listed(row: LibraryFile) -> MissingFileRow:
    return MissingFileRow(
        id=row.id,
        file_path=row.file_path,
        artist_name=None,
        track_title=None,
        release_title=None,
        missing_since=None,
        work_id=row.work_id,
        work_title="Kiss",
        match_count=2,
        work_has_present_file=True,
    )


def test_an_ambiguous_row_lists_every_ready_successor_in_path_order() -> None:
    repos, listing = fake_repos(), FakeMissingFileListingRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    b = add_file(repos, "/m/copy-b.flac", missing=False)
    a = add_file(repos, "/m/copy-a.flac", missing=False)
    add_file(repos, "/m/ungrouped.flac", missing=False, work_id=None)
    listing.seed([_listed(gone)])

    page = list_missing_files(0, 50, listing, repos.files)

    assert [c.id for c in page.entries[0].candidates] == [a.id, b.id]
    assert page.entries[0].row.match_count == 2
    assert (page.total, page.total_match_count) == (1, 2)


def test_a_row_with_no_present_copy_lists_no_candidates() -> None:
    repos, listing = fake_repos(), FakeMissingFileListingRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True)
    listing.seed([_listed(gone)])

    page = list_missing_files(0, 50, listing, repos.files)

    assert page.entries[0].candidates == ()


def test_a_row_waiting_for_a_fingerprint_lists_its_candidates() -> None:
    repos, listing = fake_repos(), FakeMissingFileListingRepository()
    add_work(repos)
    gone = add_file(repos, "/m/gone.flac", missing=True, audio_hash=_HASH)
    twin = add_file(repos, "/m/comp/twin.flac", missing=False, audio_hash=_HASH)
    copy = add_file(repos, "/m/album/copy.mp3", missing=False, file_format="mp3")
    listing.seed([_listed(gone)])
    ready = ready_successors(gone, successor_candidates(gone, repos.files))
    assert choose_successor(gone, ready) is None  # PR B waits for copy.mp3's fingerprint

    page = list_missing_files(0, 50, listing, repos.files)

    assert [c.id for c in page.entries[0].candidates] == [copy.id, twin.id]
