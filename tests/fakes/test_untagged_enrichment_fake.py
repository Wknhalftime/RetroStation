"""The fake untagged-candidate listing applies the same filters as the Pg join."""

from __future__ import annotations

from uuid import uuid4

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import AudioMetadata, LibraryFile
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.untagged_enrichment import FakeUntaggedEnrichment
from tests.fakes.works import FakeWorkRepository

_MBID = "f9776598-2689-41db-98d3-829f5510021c"


def _file(
    path: str,
    work_id: str | None,
    *,
    status: EnrichmentStatus = EnrichmentStatus.PENDING,
    recording_mbid: str | None = None,
    file_status: FileStatus = FileStatus.PRESENT,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        format="flac",
        enrichment_status=status,
        file_status=file_status,
        work_id=work_id,
        audio=AudioMetadata(track_title="Right Here, Right Now", recording_mbid=recording_mbid),
    )


def test_lists_untagged_present_files_whose_artist_has_an_mbid() -> None:
    files, works, artists = (
        FakeLibraryFileRepository(),
        FakeWorkRepository(),
        FakeArtistRepository(),
    )
    linked = artists.upsert_musicbrainz_artist(_MBID, "Jesus Jones", "Jesus Jones", "jesus jones")
    unlinked = artists.upsert_local_artist("Nobody", "nobody")
    linked_work = works.create_local("Right Here, Right Now", linked)
    unlinked_work = works.create_local("Right Here, Right Now", unlinked)

    second = files.upsert(_file("/b.flac", linked_work))
    wanted = files.upsert(_file("/a.flac", linked_work))
    files.upsert(_file("/c.flac", linked_work, recording_mbid="some-mbid"))
    files.upsert(_file("/d.flac", unlinked_work))
    files.upsert(_file("/e.flac", linked_work, status=EnrichmentStatus.ENRICHED))
    files.upsert(_file("/f.flac", linked_work, file_status=FileStatus.MISSING))
    files.upsert(_file("/g.flac", None))
    files.upsert(_file("/h.flac", linked_work, status=EnrichmentStatus.FAILED))

    listed = FakeUntaggedEnrichment(files, works, artists).list_untagged_with_linked_artist()

    assert [(c.file.id, c.artist_mbid) for c in listed] == [
        (wanted.id, _MBID),
        (second.id, _MBID),
    ]
