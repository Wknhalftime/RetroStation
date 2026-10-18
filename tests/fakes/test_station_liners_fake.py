"""Station liners, PR L1 Task 2: ``FakeStationLiners`` keeps the port contract the
PostgreSQL adapter keeps (``tests/integration/test_station_liners_pg.py``), so service tests
on the fake mean what they say.

Requirements: LR3 (stored per station, the same audio once per station, rename, delete with
its edit), LR4 (cue state from the cue store's row and the liner's own edit; liners needing
analysis). Pre-flight rulings: (a) ``needing_analysis`` gives ``path`` as
``"<station_id>/<file_name>"``; (b) the cue fake is unchanged, and reads the fake's analyses.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from backend.domain.library import AudioHash
from backend.domain.liners import (
    DuplicateLinerError,
    LinerCueState,
    StationLiner,
    liner_markers,
)
from backend.domain.streaming import ClipFormat, CueAnalysis, CueCandidate, CuePoints
from backend.repositories.station_liners import (
    LinerCueWorkRepository,
    StationLinerRepository,
)
from tests.fakes.station_liners import FakeStationLiners
from tests.fakes.stream_cues import FakeStreamCueRepository

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
POINTS = CuePoints(120, 6_900, 0, 400, 300, -3.5)


def new_hash() -> AudioHash:
    return AudioHash.parse(f"flac-md5:{uuid4().hex}")


def make_liner(
    station_id: UUID,
    *,
    name: str = "WKRP ID",
    audio: AudioHash | None = None,
    duration_ms: int = 7_000,
    uploaded_at: datetime = T0,
) -> StationLiner:
    liner_id = uuid4()
    return StationLiner(
        id=liner_id,
        station_id=station_id,
        name=name,
        file_name=f"{liner_id}.flac",
        format=ClipFormat.FLAC,
        audio_hash=audio or new_hash(),
        duration_ms=duration_ms,
        markers=None,
        uploaded_at=uploaded_at,
    )


def analysis(audio: AudioHash, *, failed: bool = False, silent: bool = False) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=audio,
        cues=POINTS,
        loudness_lufs=None,
        analysis_failed=failed,
        analyser_version=1,
        audio_silent=silent,
    )


def test_fake_implements_both_ports() -> None:
    """LR3, LR4: the fake stands in for both the liner store and the liner cue work."""
    fake = FakeStationLiners()
    assert isinstance(fake, StationLinerRepository)
    assert isinstance(fake, LinerCueWorkRepository)
    assert fake.liners == {}


def test_fake_matches_the_port_contract() -> None:
    """LR3, LR4, LR5: the duplicate, ordering and cue-state rules of the Pg adapter."""
    cues = FakeStreamCueRepository()
    fake = FakeStationLiners(cues)
    station_id, other_station = uuid4(), uuid4()
    later = make_liner(station_id, name="Jingle", uploaded_at=T0 + timedelta(minutes=5))
    earlier = make_liner(station_id, name="jingle", uploaded_at=T0)
    auto = make_liner(station_id, name="A legal ID")
    edited = make_liner(station_id, name="c ID")
    failed = make_liner(station_id, name="Drop")
    silent = make_liner(station_id, name="empty")
    for liner in (later, earlier, auto, edited, failed, silent):
        fake.add(liner)
    twin = make_liner(other_station, name="B other station", audio=auto.audio_hash)
    fake.add(twin)  # the same audio in another station is another liner
    cues.upsert(analysis(auto.audio_hash))
    cues.upsert(analysis(edited.audio_hash))
    cues.upsert(analysis(failed.audio_hash, failed=True))
    cues.upsert(analysis(silent.audio_hash, failed=True, silent=True))
    fake.save_markers(edited.id, liner_markers(0, 5_000, 7_000))

    with pytest.raises(DuplicateLinerError):
        fake.add(make_liner(station_id, name="Again", audio=auto.audio_hash))
    entries = fake.list_for_station(station_id)

    assert len(fake.liners) == 7
    assert [(e.liner.id, e.cue_state) for e in entries] == [
        (auto.id, LinerCueState.AUTO),
        (edited.id, LinerCueState.EDITED),
        (failed.id, LinerCueState.FAILED),
        (silent.id, LinerCueState.SILENT),
        (earlier.id, LinerCueState.ANALYSING),
        (later.id, LinerCueState.ANALYSING),
    ]
    assert fake.find_by_hash(station_id, auto.audio_hash) == auto
    assert fake.find_by_hash(other_station, auto.audio_hash) == twin
    assert fake.find_by_hash(uuid4(), auto.audio_hash) is None


def test_fake_rename_markers_and_delete() -> None:
    """LR3, LR4: rename changes only the name; an edit is saved and cleared; delete removes
    the liner with its edit."""
    fake = FakeStationLiners()
    liner = make_liner(uuid4())
    fake.add(liner)

    fake.rename(liner.id, "Legal ID")
    fake.save_markers(liner.id, liner_markers(0, 5_000, 7_000))
    edited = fake.get(liner.id)
    fake.save_markers(liner.id, None)
    cleared = fake.get(liner.id)
    fake.delete(liner.id)

    assert edited == replace(liner, name="Legal ID", markers=liner_markers(0, 5_000, 7_000))
    assert cleared == replace(liner, name="Legal ID")
    assert fake.get(liner.id) is None
    assert fake.liners == {}


def test_fake_needing_analysis_lists_unanalysed_liners_only() -> None:
    """LR4, rulings (a) and (b): liners whose audio the cue fake has no analysis for, at
    ``<station_id>/<file_name>``, keyed by the liner id, with no stored stat."""
    cues = FakeStreamCueRepository()
    fake = FakeStationLiners(cues)
    station_id = uuid4()
    waiting = make_liner(station_id, duration_ms=9_500)
    analysed = make_liner(station_id)
    fake.add(waiting)
    fake.add(analysed)
    cues.upsert(analysis(analysed.audio_hash))

    assert fake.needing_analysis() == [
        CueCandidate(
            file_id=waiting.id,
            path=f"{station_id}/{waiting.file_name}",
            audio_hash=waiting.audio_hash,
            duration_ms=9_500,
            file_size=None,
            file_mtime_ns=None,
        )
    ]
