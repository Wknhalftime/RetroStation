from __future__ import annotations

from abc import ABC, abstractmethod

from backend.domain.endings import EndingCandidate, SongEnding
from backend.domain.library import AudioHash


class StreamEndingRepository(ABC):
    """Stores one measured ending per audio (``AudioHash``) for the EOM ending rules."""

    @abstractmethod
    def get(self, audio_hash: AudioHash) -> SongEnding | None:
        """The ending stored for ``audio_hash``, or None when there is none (a stored row that
        fails validation also reads as None; the adapter logs it)."""
        ...

    @abstractmethod
    def needing_analysis(self, after: AudioHash | None, limit: int) -> list[EndingCandidate]:
        """Up to ``limit`` audio, ordered by hash and after ``after``, whose ``stream_cues`` row
        is analysed (not failed, not silent), that a present library file carries, and that has
        no ending or one measured against another Stop. One path per audio: the smallest."""
        ...

    @abstractmethod
    def store(self, audio_hash: AudioHash, ending: SongEnding) -> None:
        """Store ``ending`` for ``audio_hash`` at ``ENDING_ANALYSER_VERSION``, replacing any."""
        ...

    @abstractmethod
    def purge_other_versions(self, current_version: int) -> None:
        """Delete the rows of any other ending analyser version."""
        ...

    @abstractmethod
    def prune_without_cues(self) -> None:
        """Delete the endings of audio that has no ``stream_cues`` row any more."""
        ...
