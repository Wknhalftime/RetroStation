from backend.domain.library import AudioHash
from backend.domain.streaming import CueAnalysis
from backend.repositories.stream_cues import StreamCueRepository


class FakeStreamCueRepository(StreamCueRepository):
    """In-memory cue store: the latest analysis per audio, readable as ``analyses``."""

    def __init__(self) -> None:
        self.analyses: dict[AudioHash, CueAnalysis] = {}

    def upsert(self, analysis: CueAnalysis) -> None:
        self.analyses[analysis.audio_hash] = analysis
