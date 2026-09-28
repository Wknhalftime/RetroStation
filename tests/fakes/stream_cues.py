from uuid import UUID

from backend.domain.streaming import CueAnalysis
from backend.repositories.stream_cues import StreamCueRepository


class FakeStreamCueRepository(StreamCueRepository):
    """In-memory cue store: the latest analysis per file, readable as ``analyses``."""

    def __init__(self) -> None:
        self.analyses: dict[UUID, CueAnalysis] = {}

    def upsert(self, analysis: CueAnalysis) -> None:
        self.analyses[analysis.file_id] = analysis
