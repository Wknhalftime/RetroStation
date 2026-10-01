from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from backend.domain.streaming import CueCandidate
from backend.repositories.stream_cue_work import CueWorkRepository
from tests.fakes.stream_cues import FakeStreamCueRepository


@dataclass(frozen=True)
class _File:
    candidate: CueCandidate
    present: bool
    playing_on: frozenset[date]


class FakeCueWorkRepository(CueWorkRepository):
    """In-memory "needs analysis" reads (D20) over the files a test adds.

    A file's audio needs analysis while the file is present, still has the hash it was added
    with, and the cue store fake holds no analysis for that audio. ``add`` also records the
    file's hash in the store fake. ``years`` is what ``logged_years`` answers;
    ``scheduled_days`` records each ``scheduled`` call's days.
    """

    def __init__(self, store: FakeStreamCueRepository) -> None:
        self._store = store
        self._files: dict[UUID, _File] = {}
        self.years: range = range(0)
        self.scheduled_days: list[tuple[date, ...]] = []

    def add(
        self, candidate: CueCandidate, *, present: bool = True, playing_on: Iterable[date] = ()
    ) -> None:
        self._files[candidate.file_id] = _File(candidate, present, frozenset(playing_on))
        self._store.file_hashes[candidate.file_id] = candidate.audio_hash

    def logged_years(self) -> range:
        return self.years

    def scheduled(self, days: Sequence[date]) -> list[CueCandidate]:
        self.scheduled_days.append(tuple(days))
        wanted = set(days)
        return _in_path_order([f.candidate for f in self._needing() if f.playing_on & wanted])

    def library(self, after_path: str | None, limit: int) -> list[CueCandidate]:
        ordered = _in_path_order([f.candidate for f in self._needing()])
        return [c for c in ordered if after_path is None or c.path > after_path][:limit]

    def _needing(self) -> list[_File]:
        return [
            f
            for f in self._files.values()
            if f.present
            and self._store.file_hashes.get(f.candidate.file_id) == f.candidate.audio_hash
            and f.candidate.audio_hash not in self._store.analyses
        ]


def _in_path_order(candidates: list[CueCandidate]) -> list[CueCandidate]:
    return sorted(candidates, key=lambda c: c.path)
