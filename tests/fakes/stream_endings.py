from __future__ import annotations

from backend.domain.endings import ENDING_ANALYSER_VERSION, EndingCandidate, SongEnding
from backend.domain.library import AudioHash
from backend.repositories.stream_endings import StreamEndingRepository


class FakeStreamEndingRepository(StreamEndingRepository):
    """In-memory ``stream_endings``, like the Pg repository.

    A test seeds ``cued`` with the analysed audio a present library file carries (what
    ``stream_cues`` and ``library_files`` would hold), and may seed or inspect ``endings`` and
    ``versions`` directly. ``events`` records the writes in order: ``"purge"``, ``"prune"``
    and ``"store:<audio hash>"``.
    """

    def __init__(self) -> None:
        self.cued: dict[AudioHash, EndingCandidate] = {}
        self.endings: dict[AudioHash, SongEnding] = {}
        self.versions: dict[AudioHash, int] = {}
        self.events: list[str] = []

    def get(self, audio_hash: AudioHash) -> SongEnding | None:
        return self.endings.get(audio_hash)

    def needing_analysis(self, after: AudioHash | None, limit: int) -> list[EndingCandidate]:
        due = [
            candidate
            for audio_hash, candidate in sorted(self.cued.items(), key=lambda kv: str(kv[0]))
            if (after is None or str(audio_hash) > str(after))
            and (
                audio_hash not in self.endings
                or self.endings[audio_hash].measured_cue_out_ms != candidate.cues.cue_out_ms
            )
        ]
        return due[:limit]

    def store(self, audio_hash: AudioHash, ending: SongEnding) -> None:
        self.endings[audio_hash] = ending
        self.versions[audio_hash] = ENDING_ANALYSER_VERSION
        self.events.append(f"store:{audio_hash}")

    def purge_other_versions(self, current_version: int) -> None:
        for audio_hash in [h for h, v in self.versions.items() if v != current_version]:
            self.endings.pop(audio_hash, None)
            self.versions.pop(audio_hash, None)
        self.events.append("purge")

    def prune_without_cues(self) -> None:
        for audio_hash in [h for h in self.endings if h not in self.cued]:
            self.endings.pop(audio_hash, None)
            self.versions.pop(audio_hash, None)
        self.events.append("prune")
