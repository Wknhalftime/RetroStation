"""Station liners, PR L1 Task 3: autocue sees liners (fakes).

Requirement LR4: autocue analyses liners, and their cues are stored by audio hash like any
song's. Plan note 14: no new hand-off; the existing run takes liners first in its priority
set (``liners() + scheduled(...)``). Plan note 3: ``CueRunPorts.liners`` defaults to None,
which is today's run exactly. Pre-flight ruling (b): the cue fake is unchanged; a liner id
registered in ``FakeStreamCueRepository.file_hashes`` is "current".

The liner work is a ``LinerCueWork`` callable returning candidates whose ``path`` is already
absolute under the liners folder (the task layer makes it so, Task 3 Interfaces).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from backend.domain.streaming import CueCandidate, StreamTiming
from backend.playout.cue_analysis import CueFile
from backend.services.streaming.cue_precompute import CueRunConfig, CueRunPorts, run_cue_analysis
from backend.services.streaming.liners import liner_folder, station_liner_folder
from tests.services.streaming.cue_library import (
    PRIORITY_DAYS,
    TODAY,
    CueRig,
    new_audio,
)
from tests.services.streaming.test_autocue import VAN_HALEN_POINTS

STATION_ID = UUID("0a1b2c3d-4e5f-4a6b-9c7d-8e9fa0b1c2d3")
LINER_ID = UUID("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab")


@pytest.fixture
def rig(tmp_path: Path) -> CueRig:
    library = tmp_path / "library"
    library.mkdir()
    return CueRig(library)


def liner_on_disk(rig: CueRig, work_dir: Path, liner_id: UUID = LINER_ID) -> CueCandidate:
    """A liner file under the liners folder, as ``LinerCueWork`` gives it: keyed by the
    liner id, its hash registered as current in the cue fake (ruling b), no stored stat."""
    folder = station_liner_folder(work_dir, STATION_ID)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{liner_id}.flac"
    path.write_bytes(b"\x00" * 64 + liner_id.bytes)
    candidate = CueCandidate(
        file_id=liner_id,
        path=str(path),
        audio_hash=new_audio(),
        duration_ms=70_000,
        file_size=None,
        file_mtime_ns=None,
    )
    rig.store.file_hashes[liner_id] = candidate.audio_hash
    return candidate


def no_commit() -> None:
    """The fakes need no commit."""


def run(rig: CueRig, liners: list[CueCandidate] | None) -> None:
    """One run with the liner work wired (or not), batches of 8."""
    if liners is None:
        ports = CueRunPorts(work=rig.work, store=rig.store, commit=no_commit, analyse=rig.analyser)
    else:
        ports = CueRunPorts(
            work=rig.work,
            store=rig.store,
            commit=no_commit,
            analyse=rig.analyser,
            liners=lambda: list(liners),
        )
    config = CueRunConfig(today=TODAY, steady=lambda: 0.0, timing=StreamTiming())
    run_cue_analysis(ports, config)


def test_liner_folders_live_under_the_work_dir(tmp_path: Path) -> None:
    """LR3, LR4 (plan note 4): liners are stored at ``<work_dir>/liners/<station_id>/``."""
    assert liner_folder(tmp_path) == tmp_path / "liners"
    assert station_liner_folder(tmp_path, STATION_ID) == tmp_path / "liners" / str(STATION_ID)


def test_run_analyses_liners_before_scheduled_songs(rig: CueRig, tmp_path: Path) -> None:
    """LR4 (plan note 14): liners come first in the priority set, ahead of today's songs,
    which stay ahead of the rest of the library."""
    unscheduled = rig.add("a-unscheduled.flac")
    scheduled = rig.add("z-today.flac", playing_on=[PRIORITY_DAYS[0]])
    liner = liner_on_disk(rig, tmp_path / "work")

    run(rig, [liner])

    assert rig.analyser.order() == [liner.path, scheduled.path, unscheduled.path]


def test_run_without_liner_port_is_unchanged(rig: CueRig) -> None:
    """LR4, LR10 (plan note 3): ports built without liners default to none, and the run is
    today's: the scheduled songs, then the library."""
    unscheduled = rig.add("a-unscheduled.flac")
    scheduled = rig.add("z-today.flac", playing_on=[PRIORITY_DAYS[0]])
    ports = CueRunPorts(work=rig.work, store=rig.store, commit=no_commit, analyse=rig.analyser)

    run(rig, None)

    assert ports.liners is None
    assert rig.analyser.order() == [scheduled.path, unscheduled.path]
    assert set(rig.store.analyses) == {scheduled.audio_hash, unscheduled.audio_hash}


def test_liner_cues_are_stored_by_hash(rig: CueRig, tmp_path: Path) -> None:
    """LR4: a liner's analysis is stored under its audio hash, as a song's would be."""
    liner = liner_on_disk(rig, tmp_path / "work")

    run(rig, [liner])

    stored = rig.store.get(liner.audio_hash)
    assert stored is not None
    assert (stored.audio_hash, stored.cues, stored.analysis_failed) == (
        liner.audio_hash,
        VAN_HALEN_POINTS,
        False,
    )
    assert rig.analyser.files == [CueFile(liner.path, 70_000)]
