from __future__ import annotations

from uuid import UUID

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import LibraryFile, UntaggedCandidate
from backend.repositories.untagged_enrichment import UntaggedEnrichmentRepository
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.works import FakeWorkRepository


def _is_untagged_candidate(f: LibraryFile) -> bool:
    return (
        f.file_status == FileStatus.PRESENT
        and f.enrichment_status == EnrichmentStatus.PENDING
        and f.audio.recording_mbid is None
        and f.audio.release_mbid is None
    )


class FakeUntaggedEnrichment(UntaggedEnrichmentRepository):
    """Answers the Pg join (file -> performance credit -> artist MBID) from the three fakes.

    The fakes model no performances, so a file's credits are its work's
    (``FakeWorkRepository.credit``, 3b S7, R4); the first artist with an MBID among them stands
    for the earliest credit with one that the Pg join reads.
    """

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
        out: dict[UUID, UntaggedCandidate] = {}
        for artist in self._artists.list_all():
            if artist.mbid is None:
                continue
            for work in self._works.works_crediting(artist.id):
                for f in self._files.get_by_work(work.id):
                    if _is_untagged_candidate(f) and f.id not in out:
                        out[f.id] = UntaggedCandidate(file=f, artist_mbid=artist.mbid)
        return sorted(out.values(), key=lambda c: c.file.file_path)
