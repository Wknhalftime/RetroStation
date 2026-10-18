"""Cue editor PR 1, Task 2: the in-memory cue-edit store behaves like the Pg one.

``FakeStreamCueEditRepository`` must honour the same contract the Pg adapter is tested
against (tests/integration/test_stream_cue_edits_pg.py), because later cue-editor tests rely
on it instead of a database. Also covers the new ``FakeStreamCueRepository.get``.
Requirement R4 (edits keyed by audio hash) and the Task 2 Interfaces.
"""

from __future__ import annotations

from uuid import uuid4

from backend.domain.library import AudioHash
from backend.domain.streaming import CueAnalysis, CueMarkers, CuePoints
from backend.repositories.stream_cue_edits import StreamCueEditRepository
from tests.fakes.stream_cue_edits import FakeStreamCueEditRepository
from tests.fakes.stream_cues import FakeStreamCueRepository

H = AudioHash.parse("flac-md5:" + "a" * 32)
H2 = AudioHash.parse("flac-md5:" + "b" * 32)
M = CueMarkers(start_ms=9_000, eom_ms=241_100, fade_at_ms=241_100, stop_ms=247_100)

HERETIC = CuePoints(
    cue_in_ms=0,
    cue_out_ms=247_100,
    fade_in_ms=0,
    fade_out_ms=6_000,
    start_next_ms=6_000,
    gain_db=-11.63,
)


def _analysis(audio_hash: AudioHash = H, analyser_version: int = 1) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=audio_hash,
        cues=HERETIC,
        loudness_lufs=-14.5,
        analysis_failed=False,
        analyser_version=analyser_version,
    )


def test_edit_fake_implements_the_port():
    """Task 2 Interfaces: the fake is a concrete StreamCueEditRepository (no abstract left)."""
    assert isinstance(FakeStreamCueEditRepository(), StreamCueEditRepository)


def test_save_then_get_round_trips():
    """R4: the markers read back as saved."""
    repo = FakeStreamCueEditRepository()

    repo.save(H, M)

    assert repo.get(H) == M


def test_save_twice_keeps_the_latest():
    """R4: a second save for the same audio replaces the first."""
    repo = FakeStreamCueEditRepository()
    later = CueMarkers(start_ms=9_000, eom_ms=241_100, fade_at_ms=241_100, stop_ms=246_000)

    repo.save(H, M)
    repo.save(H, later)

    assert repo.get(H) == later


def test_get_unknown_hash_is_none():
    """R4: audio with no edit has none."""
    assert FakeStreamCueEditRepository().get(H) is None


def test_delete_removes_and_is_idempotent():
    """R4: delete removes the named audio's edit only, and deleting an absent one is fine."""
    repo = FakeStreamCueEditRepository()
    repo.save(H, M)
    repo.save(H2, M)

    repo.delete(H)
    repo.delete(H)  # absent now: must not raise

    assert repo.get(H) is None
    assert repo.get(H2) == M


def test_edits_dict_is_the_store():
    """Task 2 Interfaces: ``edits`` maps AudioHash to CueMarkers, for seeding and inspection."""
    repo = FakeStreamCueEditRepository()

    repo.save(H, M)
    assert repo.edits == {H: M}

    repo.edits[H2] = M
    assert repo.get(H2) == M


def test_cue_fake_get_returns_what_upsert_stored():
    """Task 2 Interfaces: ``FakeStreamCueRepository.get`` reads back the stored analysis."""
    repo = FakeStreamCueRepository()
    analysis = _analysis()

    repo.upsert(analysis)

    assert repo.get(H) == analysis


def test_cue_fake_get_returns_the_latest_upsert():
    """Task 2 Interfaces: a later upsert for the same audio replaces the earlier one."""
    repo = FakeStreamCueRepository()
    repo.upsert(_analysis(analyser_version=1))
    newer = _analysis(analyser_version=2)

    repo.upsert(newer)

    assert repo.get(H) == newer


def test_cue_fake_get_unknown_is_none():
    """Task 2 Interfaces: no analysis stored, none returned."""
    repo = FakeStreamCueRepository()
    repo.upsert(_analysis(H2))

    assert repo.get(H) is None


def test_cue_fake_get_sees_a_conditional_store():
    """Task 2 Interfaces: ``get`` reads the same store ``store_if_current`` writes."""
    repo = FakeStreamCueRepository()
    file_id = uuid4()
    repo.file_hashes[file_id] = H
    analysis = _analysis()

    assert repo.store_if_current(analysis, file_id) is True

    assert repo.get(H) == analysis


def test_cue_fake_get_after_purge_is_none():
    """Task 2 Interfaces: an analysis purged by version is no longer returned."""
    repo = FakeStreamCueRepository()
    repo.upsert(_analysis(analyser_version=1))

    repo.purge_other_versions(2)

    assert repo.get(H) is None
