"""Behaviour of the cue store and cue work fakes, which the pipeline tests rely on. They
mirror the SQL that the integration tests pin (spec: D20 needs analysis; "stores nothing if
the hash changed meanwhile"; D20 purge; D56 two-strike prune)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from backend.domain.library import AudioHash
from backend.domain.streaming import CUE_ANALYSER_VERSION, CueAnalysis, CueCandidate, CuePoints
from tests.fakes.stream_cue_work import FakeCueWorkRepository
from tests.fakes.stream_cues import FakeStreamCueRepository

DAY = date(1995, 3, 14)
T0 = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
GRACE = timedelta(hours=24)
POINTS = CuePoints(
    cue_in_ms=0,
    cue_out_ms=100_000,
    fade_in_ms=1_000,
    fade_out_ms=1_000,
    start_next_ms=1_000,
    gain_db=-2.0,
)


def audio() -> AudioHash:
    return AudioHash.parse(f"flac-md5:{uuid4().hex}")


def candidate(path: str, sound: AudioHash | None = None) -> CueCandidate:
    return CueCandidate(
        file_id=uuid4(),
        path=path,
        audio_hash=sound or audio(),
        duration_ms=200_000,
        file_size=1,
        file_mtime_ns=1,
    )


def analysis(sound: AudioHash, version: int = CUE_ANALYSER_VERSION) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=sound,
        cues=POINTS,
        loudness_lufs=None,
        analysis_failed=False,
        analyser_version=version,
    )


def test_the_work_fake_is_concrete() -> None:
    """Project rule: fakes implement the repository ABCs."""
    assert FakeCueWorkRepository(FakeStreamCueRepository()) is not None


def test_store_if_current_needs_the_file_to_still_have_the_hash() -> None:
    """For PR D and PR E: "stores nothing if the hash changed meanwhile"."""
    store = FakeStreamCueRepository()
    file_id, sound = uuid4(), audio()
    store.file_hashes[file_id] = None
    assert store.store_if_current(analysis(sound), file_id) is False
    store.file_hashes[file_id] = sound
    assert store.store_if_current(analysis(sound), file_id) is True
    assert store.analyses == {sound: analysis(sound)}


def test_purge_drops_rows_of_other_versions() -> None:
    """D20: an analyser change purges its old rows once."""
    store = FakeStreamCueRepository()
    old, current = audio(), audio()
    store.upsert(analysis(old, version=CUE_ANALYSER_VERSION + 1))
    store.upsert(analysis(current))
    store.purge_other_versions(CUE_ANALYSER_VERSION)
    assert set(store.analyses) == {current}


def test_prune_deletes_an_orphan_only_at_the_second_strike() -> None:
    """D56: marked at the first daily prune, deleted at one at least 24 h later."""
    store = FakeStreamCueRepository()
    orphan = audio()
    store.upsert(analysis(orphan))
    store.prune_orphans(T0, GRACE)
    assert (orphan in store.analyses, store.orphaned) == (True, {orphan: T0})
    store.prune_orphans(T0 + timedelta(hours=23), GRACE)
    assert orphan in store.analyses
    store.prune_orphans(T0 + GRACE, GRACE)
    assert (orphan in store.analyses, store.orphaned) == (False, {})


def test_prune_clears_a_mark_when_the_hash_reappears() -> None:
    """D56: a retagged MP3 re-hashed by the backfill keeps its cues."""
    store = FakeStreamCueRepository()
    back = audio()
    store.upsert(analysis(back))
    store.prune_orphans(T0, GRACE)
    store.file_hashes[uuid4()] = back
    store.prune_orphans(T0 + GRACE, GRACE)
    assert (back in store.analyses, store.orphaned) == (True, {})


def test_an_upsert_clears_the_orphan_mark() -> None:
    """D66: "An upsert clears orphaned_at"."""
    store = FakeStreamCueRepository()
    marked = audio()
    store.upsert(analysis(marked))
    store.prune_orphans(T0, GRACE)
    store.upsert(analysis(marked))
    assert store.orphaned == {}


def test_the_library_read_lists_needing_audio_in_path_order_after_the_cursor() -> None:
    """D20: needs analysis = present, hashed, no row; keyset by path (D63 batches)."""
    store = FakeStreamCueRepository()
    work = FakeCueWorkRepository(store)
    for name in ("c", "a", "b"):
        work.add(candidate(f"D:/m/{name}.flac"))
    cued, gone = candidate("D:/m/d.flac"), candidate("D:/m/e.flac")
    work.add(cued)
    work.add(gone, present=False)
    store.upsert(analysis(cued.audio_hash))
    assert [c.path for c in work.library(None, 10)] == [
        "D:/m/a.flac",
        "D:/m/b.flac",
        "D:/m/c.flac",
    ]
    assert [c.path for c in work.library("D:/m/a.flac", 1)] == ["D:/m/b.flac"]


def test_the_scheduled_read_lists_only_audio_playing_on_the_days() -> None:
    """D62: the priority set is the audio playing on the given days, read once."""
    work = FakeCueWorkRepository(FakeStreamCueRepository())
    today, other = candidate("D:/m/a.flac"), candidate("D:/m/b.flac")
    work.add(today, playing_on=[DAY])
    work.add(other, playing_on=[date(1995, 3, 20)])
    assert work.scheduled([DAY]) == [today]
    assert work.scheduled_days == [(DAY,)]
