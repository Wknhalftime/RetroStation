from datetime import datetime, timedelta
from uuid import UUID

from backend.domain.library import AudioHash
from backend.domain.streaming import CueAnalysis
from backend.repositories.stream_cues import StreamCueRepository


class FakeStreamCueRepository(StreamCueRepository):
    """In-memory cue store (D20): the latest analysis per audio (``analyses``) and the
    two-strike orphan marks (``orphaned``, D56), standing in for stream_cues.orphaned_at
    (D66); an upsert clears a mark.

    ``file_hashes`` stands in for ``library_files.audio_hash`` (any status). The conditional
    write and the prune read it, so a test changes a file's hash here to simulate a rescan.
    """

    def __init__(self) -> None:
        self.analyses: dict[AudioHash, CueAnalysis] = {}
        self.orphaned: dict[AudioHash, datetime] = {}
        self.file_hashes: dict[UUID, AudioHash | None] = {}

    def upsert(self, analysis: CueAnalysis) -> None:
        self.analyses[analysis.audio_hash] = analysis
        self.orphaned.pop(analysis.audio_hash, None)

    def store_if_current(self, analysis: CueAnalysis, file_id: UUID) -> bool:
        if self.file_hashes.get(file_id) != analysis.audio_hash:
            return False
        self.upsert(analysis)
        return True

    def purge_other_versions(self, current_version: int) -> None:
        for audio, row in list(self.analyses.items()):
            if row.analyser_version != current_version:
                del self.analyses[audio]
                self.orphaned.pop(audio, None)

    def prune_orphans(self, now: datetime, grace: timedelta) -> None:
        library = {audio for audio in self.file_hashes.values() if audio is not None}
        for audio in [a for a in self.orphaned if a in library]:
            del self.orphaned[audio]
        for audio, marked in list(self.orphaned.items()):
            if now - marked >= grace:
                del self.orphaned[audio]
                self.analyses.pop(audio, None)
        for audio in self.analyses:
            if audio not in library:
                self.orphaned.setdefault(audio, now)
