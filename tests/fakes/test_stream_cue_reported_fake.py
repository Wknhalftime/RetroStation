"""The cue work fake's ``reported`` answers as PostgreSQL does (spec: D20 needs analysis;
D21 present only; D61 a failed row is data; D79 the owner analyses only audio with no data)."""

from __future__ import annotations

from uuid import uuid4

from backend.domain.library import AudioHash
from backend.domain.streaming import CUE_ANALYSER_VERSION, CueAnalysis, CueCandidate
from tests.fakes.stream_cue_work import FakeCueWorkRepository
from tests.fakes.stream_cues import FakeStreamCueRepository
from tests.services.streaming.test_autocue import VAN_HALEN_POINTS


def candidate() -> CueCandidate:
    return CueCandidate(
        file_id=uuid4(),
        path=f"D:/Music/{uuid4()}.flac",
        audio_hash=AudioHash.parse(f"flac-md5:{uuid4().hex}"),
        duration_ms=200_000,
        file_size=4_000_000,
        file_mtime_ns=1,
    )


def test_a_file_whose_audio_needs_analysis_is_reported_as_its_candidate() -> None:
    work = FakeCueWorkRepository(FakeStreamCueRepository())
    wanted = candidate()
    work.add(wanted)
    assert work.reported(wanted.file_id) == wanted


def test_nothing_with_a_row_a_changed_hash_an_absent_or_an_unknown_file() -> None:
    store = FakeStreamCueRepository()
    work = FakeCueWorkRepository(store)
    failed, rehashed, absent = candidate(), candidate(), candidate()
    work.add(failed)
    work.add(rehashed)
    work.add(absent, present=False)
    store.upsert(CueAnalysis(failed.audio_hash, VAN_HALEN_POINTS, None, True, CUE_ANALYSER_VERSION))
    store.file_hashes[rehashed.file_id] = None
    asked = (failed.file_id, rehashed.file_id, absent.file_id, uuid4())
    assert [work.reported(file_id) for file_id in asked] == [None, None, None, None]
