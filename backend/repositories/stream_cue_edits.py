from __future__ import annotations

from abc import ABC, abstractmethod

from backend.domain.library import AudioHash
from backend.domain.streaming import CueMarkers


class StreamCueEditRepository(ABC):
    """Stores one manual cue edit per audio (``AudioHash``) for tune-in streaming (R4).

    Separate from the analyser's cue cache: the analyser purge and the orphan prune never
    touch an edit.
    """

    @abstractmethod
    def get(self, audio_hash: AudioHash) -> CueMarkers | None:
        """The edit stored for ``audio_hash``, or None when the audio has none."""
        ...

    @abstractmethod
    def save(self, audio_hash: AudioHash, markers: CueMarkers) -> None:
        """Store ``markers`` as the audio's edit, replacing any earlier one."""
        ...

    @abstractmethod
    def delete(self, audio_hash: AudioHash) -> None:
        """Remove the audio's edit; not an error when there is none."""
        ...
