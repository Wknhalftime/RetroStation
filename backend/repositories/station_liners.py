from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.library import AudioHash
from backend.domain.liners import LinerEntry, PlayableLiner, StationLiner
from backend.domain.streaming import CueCandidate, CueMarkers


class StationLinerRepository(ABC):
    """Stores each station's liners (LR3): one row per liner, the same audio once per
    station, and each liner's own manual cue edit (LR4)."""

    @abstractmethod
    def add(self, liner: StationLiner) -> None:
        """Store ``liner``. Raises ``DuplicateLinerError`` when its station already has a
        liner with this audio (the unique key)."""
        ...

    @abstractmethod
    def get(self, liner_id: UUID) -> StationLiner | None: ...

    @abstractmethod
    def find_by_hash(self, station_id: UUID, audio_hash: AudioHash) -> StationLiner | None:
        """The station's liner carrying ``audio_hash``, or None."""
        ...

    @abstractmethod
    def list_for_station(self, station_id: UUID) -> list[LinerEntry]:
        """The station's liners by ``lower(name)``, then ``uploaded_at``, each with the
        state of its cues (``liner_cue_state`` of its edit and its audio's cue row)."""
        ...

    @abstractmethod
    def rename(self, liner_id: UUID, name: str) -> None:
        """Change the liner's name only, stripped as ``StationLiner`` strips it. Raises
        ``InvalidLinerValueError`` for a name a ``StationLiner`` refuses; nothing when there
        is no such liner."""
        ...

    @abstractmethod
    def save_markers(self, liner_id: UUID, markers: CueMarkers | None) -> None:
        """Store ``markers`` as the liner's edit, or clear it with None. Raises
        ``InvalidLinerValueError`` for markers a ``StationLiner`` refuses (the fade not at
        Stop, or Stop past the duration); nothing when there is no such liner."""
        ...

    @abstractmethod
    def delete(self, liner_id: UUID) -> None:
        """Remove the liner and its edit; not an error when there is none."""
        ...


class LinerCueWorkRepository(ABC):
    """What cue pre-computation reads about liners (LR4)."""

    @abstractmethod
    def needing_analysis(self) -> list[CueCandidate]:
        """Every liner whose audio has no ``stream_cues`` row, keyed by the liner id, at
        ``"<station_id>/<file_name>"`` relative to the liners root, with no stored stat;
        in path order."""
        ...


class PlayableLinerRepository(ABC):
    """What the stream reads about a station's liners (LR3, LR4)."""

    @abstractmethod
    def playable(self, station_id: UUID) -> list[PlayableLiner]:
        """The station's liners the radio can play, each with its ``liner_points`` (its edit
        and its audio's ``stream_cues`` row); a silent liner is left out. Empty for a station
        with none, or no such station."""
        ...
