from __future__ import annotations

from backend.domain.library import AudioHash
from backend.domain.streaming import CueMarkers
from backend.repositories.stream_cue_edits import StreamCueEditRepository


class FakeStreamCueEditRepository(StreamCueEditRepository):
    """In-memory cue-edit store: the latest edit per audio (``edits``), like
    ``stream_cue_edits``. A test may seed or inspect ``edits`` directly."""

    def __init__(self) -> None:
        self.edits: dict[AudioHash, CueMarkers] = {}

    def get(self, audio_hash: AudioHash) -> CueMarkers | None:
        return self.edits.get(audio_hash)

    def save(self, audio_hash: AudioHash, markers: CueMarkers) -> None:
        self.edits[audio_hash] = markers

    def delete(self, audio_hash: AudioHash) -> None:
        self.edits.pop(audio_hash, None)
