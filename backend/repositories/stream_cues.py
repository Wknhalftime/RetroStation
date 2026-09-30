from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from uuid import UUID

from backend.domain.streaming import CueAnalysis


class StreamCueRepository(ABC):
    """Stores one cue analysis per audio (``AudioHash``) for tune-in streaming (D20)."""

    @abstractmethod
    def upsert(self, analysis: CueAnalysis) -> None:
        """Store ``analysis`` as its audio's cues, replacing any earlier analysis.

        Clears the orphan mark (D66). Kept for the locked PR C tests (N3); no E1
        production code calls it: cue pre-computation writes through ``store_if_current``.
        """
        ...

    @abstractmethod
    def store_if_current(self, analysis: CueAnalysis, file_id: UUID) -> bool:
        """Store ``analysis`` as its audio's cues only while ``file_id`` still carries
        ``analysis.audio_hash``; True if stored."""
        ...

    @abstractmethod
    def purge_other_versions(self, current_version: int) -> None:
        """Delete the rows of any other analyser version (D20)."""
        ...

    @abstractmethod
    def prune_orphans(self, now: datetime, grace: timedelta) -> None:
        """The daily two-strike prune (D56), in order:

        1. clear the marks of audio a library file (any status) carries again;
        2. delete rows marked at or before ``now - grace``;
        3. mark every other row whose audio no library file carries, with ``now``,
           keeping earlier marks.
        """
        ...
