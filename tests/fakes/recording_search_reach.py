import sys

from backend.repositories.recording_search_reach import RecordingSearchReachRepository
from tests.fakes.library_files import FakeLibraryFileRepository


class FakeRecordingSearchReach(RecordingSearchReachRepository):
    """Answers from a FakeLibraryFileRepository's present files; records each question."""

    def __init__(self, files: FakeLibraryFileRepository) -> None:
        self._files = files
        self.queries: list[tuple[str, str]] = []

    def has_file_beyond_artist_mbid(self, normalized_artist_name: str, artist_mbid: str) -> bool:
        self.queries.append((normalized_artist_name, artist_mbid))
        present = self._files.get_by_normalized_artist_name(normalized_artist_name, sys.maxsize)
        return any(
            f.audio.recording_mbid is not None
            and artist_mbid not in (f.audio.artist_mbid, f.audio.album_artist_mbid)
            for f in present
        )
