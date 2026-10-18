from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from backend.domain.library import AudioHash
from backend.domain.liners import (
    DuplicateLinerError,
    LinerEntry,
    PlayableLiner,
    StationLiner,
    liner_cue_state,
    liner_points,
)
from backend.domain.streaming import CueCandidate, CueMarkers
from backend.repositories.station_liners import (
    LinerCueWorkRepository,
    PlayableLinerRepository,
    StationLinerRepository,
)
from tests.fakes.stream_cues import FakeStreamCueRepository


class FakeStationLiners(StationLinerRepository, LinerCueWorkRepository, PlayableLinerRepository):
    """In-memory liners (``liners``), keeping the PostgreSQL adapter's rules: the same audio
    once per station, the list order, and cue state, needing-analysis and playable points read
    from the cue fake's ``analyses`` (pass the fake the test analyses through; a fresh one by
    default). ``playable`` lists liners in the order they were stored."""

    def __init__(self, cues: FakeStreamCueRepository | None = None) -> None:
        self.liners: dict[UUID, StationLiner] = {}
        self._cues = cues if cues is not None else FakeStreamCueRepository()

    def add(self, liner: StationLiner) -> None:
        if self.find_by_hash(liner.station_id, liner.audio_hash) is not None:
            raise DuplicateLinerError(
                f"station {liner.station_id} already has a liner with {liner.audio_hash}"
            )
        self.liners[liner.id] = liner

    def get(self, liner_id: UUID) -> StationLiner | None:
        return self.liners.get(liner_id)

    def find_by_hash(self, station_id: UUID, audio_hash: AudioHash) -> StationLiner | None:
        return next(
            (
                x
                for x in self.liners.values()
                if (x.station_id, x.audio_hash) == (station_id, audio_hash)
            ),
            None,
        )

    def list_for_station(self, station_id: UUID) -> list[LinerEntry]:
        mine = [x for x in self.liners.values() if x.station_id == station_id]
        mine.sort(key=lambda x: (x.name.lower(), x.uploaded_at))
        return [
            LinerEntry(x, liner_cue_state(x.markers, self._cues.get(x.audio_hash))) for x in mine
        ]

    def rename(self, liner_id: UUID, name: str) -> None:
        liner = self.liners.get(liner_id)
        if liner is not None:
            self.liners[liner_id] = replace(liner, name=name)

    def save_markers(self, liner_id: UUID, markers: CueMarkers | None) -> None:
        liner = self.liners.get(liner_id)
        if liner is not None:
            self.liners[liner_id] = replace(liner, markers=markers)

    def delete(self, liner_id: UUID) -> None:
        self.liners.pop(liner_id, None)

    def needing_analysis(self) -> list[CueCandidate]:
        waiting = [x for x in self.liners.values() if self._cues.get(x.audio_hash) is None]
        waiting.sort(key=lambda x: (x.station_id, x.file_name))
        return [
            CueCandidate(
                file_id=x.id,
                path=f"{x.station_id}/{x.file_name}",
                audio_hash=x.audio_hash,
                duration_ms=x.duration_ms,
                file_size=None,
                file_mtime_ns=None,
            )
            for x in waiting
        ]

    def playable(self, station_id: UUID) -> list[PlayableLiner]:
        found = (
            (x, liner_points(x.duration_ms, x.markers, self._cues.get(x.audio_hash)))
            for x in self.liners.values()
            if x.station_id == station_id
        )
        return [PlayableLiner(x.id, x.file_name, cues) for x, cues in found if cues is not None]
