"""The listing fake pages the rows a test seeds, in path order, with totals over all."""

from __future__ import annotations

from uuid import uuid4

from backend.domain.library import MissingFileRow
from tests.fakes.missing_files import FakeMissingFileListingRepository


def _row(path: str, matches: int) -> MissingFileRow:
    return MissingFileRow(
        id=uuid4(),
        file_path=path,
        artist_name=None,
        track_title=None,
        release_title=None,
        missing_since=None,
        work_id=None,
        work_title=None,
        match_count=matches,
        work_has_present_file=False,
    )


def test_fake_listing_pages_seeded_rows_in_path_order() -> None:
    listing = FakeMissingFileListingRepository()
    listing.seed([_row("/m/c.flac", 1), _row("/m/a.flac", 2), _row("/m/b.flac", 0)])

    page = listing.list_page(1, 1)

    assert [r.file_path for r in page.rows] == ["/m/b.flac"]
    assert (page.total, page.total_match_count) == (3, 3)
