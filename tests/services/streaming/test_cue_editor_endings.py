"""The cue editor opens on the ruled markers (EOM ending rules, stage 1, Task 8).

The editor's promise (cue editor spec R3, C2) is that a song opens on what the radio plays for
it. With the ending rules on, that is the rule's song-to-song points over autocue's row, so the
editor opens there and Reset (which deletes the edit) returns there. A manual edit is still
reported as it was saved. Ports built without an endings store open on autocue, as before.
Fakes only, no database.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from backend.domain.endings import EndingType, SongEnding
from backend.domain.enums import FileStatus
from backend.domain.library import AudioHash, AudioMetadata, LibraryFile
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CueMarkers,
    CuePoints,
    StreamTiming,
)
from backend.services.streaming.cue_editor import CueEditorPorts, read_cue_editor
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.stream_cue_edits import FakeStreamCueEditRepository
from tests.fakes.stream_cues import FakeStreamCueRepository
from tests.fakes.stream_endings import FakeStreamEndingRepository

TIMING = StreamTiming()
FILE_ID = UUID("00000000-0000-0000-0000-0000000000e1")
HASH = AudioHash.parse("flac-md5:" + "e" * 32)
STORED = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=0,
    fade_out_ms=4_000,
    start_next_ms=5_000,
    gain_db=-6.0,
)
AUTOCUE_OPENS_AS = CueMarkers(start_ms=0, eom_ms=195_000, fade_at_ms=196_000, stop_ms=200_000)


@pytest.fixture
def ports(tmp_path: Path) -> CueEditorPorts:
    files = FakeLibraryFileRepository()
    files.upsert(
        LibraryFile(
            id=FILE_ID,
            file_path=str(tmp_path / "song.flac"),
            format="flac",
            file_status=FileStatus.PRESENT,
            audio_hash=HASH,
            audio=AudioMetadata(track_title="Song", artist_name="Artist", duration_ms=205_000),
        )
    )
    cues = FakeStreamCueRepository()
    cues.upsert(CueAnalysis(HASH, STORED, -14.0, False, CUE_ANALYSER_VERSION))
    return CueEditorPorts(
        files, cues, FakeStreamCueEditRepository(), endings=FakeStreamEndingRepository()
    )


def endings(ports: CueEditorPorts) -> FakeStreamEndingRepository:
    assert isinstance(ports.endings, FakeStreamEndingRepository)
    return ports.endings


def test_opens_on_long_tail_markers(ports: CueEditorPorts) -> None:
    endings(ports).endings[HASH] = SongEnding(EndingType.LONG_TAIL, 190_500, 200_000)
    view = read_cue_editor(ports, FILE_ID, TIMING)
    assert view.automatic == CueMarkers(
        start_ms=0, eom_ms=190_500, fade_at_ms=190_500, stop_ms=192_500
    )


def test_opens_on_other_markers(ports: CueEditorPorts) -> None:
    endings(ports).endings[HASH] = SongEnding(EndingType.OTHER, None, 200_000)
    view = read_cue_editor(ports, FILE_ID, TIMING)
    assert view.automatic == CueMarkers(
        start_ms=0, eom_ms=195_000, fade_at_ms=195_000, stop_ms=196_000
    )


def test_opens_on_cold_markers(ports: CueEditorPorts) -> None:
    endings(ports).endings[HASH] = SongEnding(EndingType.COLD, None, 200_000)
    view = read_cue_editor(ports, FILE_ID, TIMING)
    assert view.automatic == CueMarkers(
        start_ms=0, eom_ms=195_000, fade_at_ms=200_000, stop_ms=200_000
    )


def test_no_ending_opens_on_autocue(ports: CueEditorPorts) -> None:
    assert read_cue_editor(ports, FILE_ID, TIMING).automatic == AUTOCUE_OPENS_AS


def test_ports_without_endings_open_on_autocue(ports: CueEditorPorts) -> None:
    plain = CueEditorPorts(ports.files, ports.cues, ports.edits)
    assert plain.endings is None
    assert read_cue_editor(plain, FILE_ID, TIMING).automatic == AUTOCUE_OPENS_AS


def test_manual_edit_still_reported(ports: CueEditorPorts) -> None:
    endings(ports).endings[HASH] = SongEnding(EndingType.LONG_TAIL, 190_500, 200_000)
    edit = CueMarkers(start_ms=1_000, eom_ms=194_000, fade_at_ms=193_000, stop_ms=199_000)
    ports.edits.save(HASH, edit)
    view = read_cue_editor(ports, FILE_ID, TIMING)
    assert view.manual == edit
    assert view.automatic == CueMarkers(
        start_ms=0, eom_ms=190_500, fade_at_ms=190_500, stop_ms=192_500
    )


def test_api_wires_the_endings_store() -> None:
    """The editor's API ports carry the request connection's endings store, so the editor
    opens on the rule in the app, not only in these fakes."""
    from types import SimpleNamespace

    from backend.dependencies import get_cue_editor_ports

    store = FakeStreamEndingRepository()
    streaming = SimpleNamespace(
        cues=FakeStreamCueRepository(), edits=FakeStreamCueEditRepository(), endings=store
    )
    repos = SimpleNamespace(library_files=FakeLibraryFileRepository(), streaming=streaming)
    assert get_cue_editor_ports(repos).endings is store  # type: ignore[arg-type]
