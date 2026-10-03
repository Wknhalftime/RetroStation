"""The in-memory cue coverage fake (PR G2; D77, PG7), beside the Pg adapter."""

from uuid import UUID

from backend.domain.library import AudioHash
from backend.domain.streaming import CueCoverage
from backend.repositories.stream_cue_coverage import CueCoverageRepository
from tests.fakes.stream_cues import FakeStreamCueRepository


class FakeCueCoverageRepository(CueCoverageRepository):
    """In-memory cue coverage (D77; PG7) over the files a test adds and the cue store fake.

    A present file with an audio hash makes its audio analysable; the store fake's row for
    that audio makes it ready, or failed when the row is a fallback (``analysis_failed``). A
    present file with no hash is unhashed. Rows for audio no file carries are not counted.
    ``reads`` counts the ``coverage`` calls.
    """

    def __init__(self, store: FakeStreamCueRepository | None = None) -> None:
        self._store = store if store is not None else FakeStreamCueRepository()
        self._files: dict[UUID, tuple[AudioHash | None, bool]] = {}
        self.reads = 0

    def add(self, file_id: UUID, audio: AudioHash | None, *, present: bool = True) -> None:
        self._files[file_id] = (audio, present)

    def coverage(self) -> CueCoverage:
        self.reads += 1
        present = [audio for audio, is_present in self._files.values() if is_present]
        analysable = {audio for audio in present if audio is not None}
        rows = {a: r for a, r in self._store.analyses.items() if a in analysable}
        failed = sum(1 for row in rows.values() if row.analysis_failed)
        return CueCoverage(
            analysable=len(analysable),
            ready=len(rows) - failed,
            failed=failed,
            unhashed=sum(1 for audio in present if audio is None),
        )
