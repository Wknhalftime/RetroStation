from __future__ import annotations

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import LibraryFile, UntaggedCandidate
from backend.repositories.untagged_enrichment import UntaggedEnrichmentRepository
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.works import FakeWorkRepository

_CANDIDATE_STATUSES = (EnrichmentStatus.PENDING, EnrichmentStatus.SKIPPED)


def _is_untagged_candidate(f: LibraryFile) -> bool:
    return (
        f.file_status == FileStatus.PRESENT
        and f.enrichment_status in _CANDIDATE_STATUSES
        and f.audio.recording_mbid is None
        and f.audio.release_mbid is None
    )


class FakeUntaggedEnrichment(UntaggedEnrichmentRepository):
    """Answers the Pg join (file -> work -> artist) from the three fakes."""

    def __init__(
        self,
        files: FakeLibraryFileRepository,
        works: FakeWorkRepository,
        artists: FakeArtistRepository,
    ) -> None:
        self._files = files
        self._works = works
        self._artists = artists

    def list_untagged_with_linked_artist(self) -> list[UntaggedCandidate]:
        out: list[UntaggedCandidate] = []
        for artist in self._artists.list_all():
            if artist.mbid is None:
                continue
            for work in self._works.get_by_artist(artist.id):
                out.extend(
                    UntaggedCandidate(file=f, artist_mbid=artist.mbid)
                    for f in self._files.get_by_work(work.id)
                    if _is_untagged_candidate(f)
                )
        return sorted(out, key=lambda c: c.file.file_path)
