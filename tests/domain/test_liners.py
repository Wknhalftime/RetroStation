"""Station liners, PR L1 Task 1: the domain values in ``backend/domain/liners.py`` and the
station's liner cadence.

Requirements: LR1 (per-station cadence: blank = off, else a whole number 1-1,000), LR3 (the
display name, stored per station), LR4 (cues: autocue's gain, the whole file before analysis,
fade always 0, a silent liner never plays, manual edits per liner). Plan notes 6 (the liner
cue points) and 7 (an edit's fade is always its Stop).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.domain.broadcast import BroadcastStation
from backend.domain.library import AudioHash
from backend.domain.liners import (
    LINER_FILE_NAME,
    MAX_CADENCE,
    MAX_LINER_BYTES,
    MAX_LINER_NAME,
    MIN_CADENCE,
    DuplicateLinerError,
    InvalidLinerValueError,
    LinerCadence,
    LinerCueState,
    LinerError,
    LinerNotFoundError,
    LinerStorageError,
    LinerTooLargeError,
    StationLiner,
    liner_cue_state,
    liner_markers,
    liner_points,
)
from backend.domain.streaming import ClipFormat, CueAnalysis, CueMarkers, CuePoints

LINER_ID = UUID("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab")
STATION_ID = UUID("0a1b2c3d-4e5f-4a6b-9c7d-8e9fa0b1c2d3")
HASH = AudioHash.parse("flac-md5:" + "c" * 32)
UPLOADED = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def liner(**changes: object) -> StationLiner:
    """A valid liner (7 s WAV, no edit); ``changes`` replaces fields before validation."""
    fields: dict[str, object] = {
        "id": LINER_ID,
        "station_id": STATION_ID,
        "name": "WKRP ID",
        "file_name": f"{LINER_ID}.wav",
        "format": ClipFormat.WAV,
        "audio_hash": HASH,
        "duration_ms": 7_000,
        "markers": None,
        "uploaded_at": UPLOADED,
    }
    fields.update(changes)
    return StationLiner(**fields)  # type: ignore[arg-type]


def row(
    cues: CuePoints, *, failed: bool = False, silent: bool = False, audio: AudioHash = HASH
) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=audio,
        cues=cues,
        loudness_lufs=None if failed else -14.0,
        analysis_failed=failed,
        analyser_version=1,
        audio_silent=silent,
    )


AUTO_ROW = row(CuePoints(120, 6_900, 0, 400, 300, -3.5))
FAILED_ROW = row(CuePoints(0, 7_000, 0, 4_000, 0, -8.0), failed=True)
SILENT_ROW = row(CuePoints(0, 7_000, 0, 4_000, 0, 0.0), failed=True, silent=True)
EDIT = CueMarkers(100, 5_000, 6_500, 6_500)


def test_errors_share_the_liner_base_and_the_limits_are_the_plans() -> None:
    """LR1, LR2 (plan note 13, 15): one base error per subdomain; the stated limits."""
    for error in (
        InvalidLinerValueError,
        LinerNotFoundError,
        DuplicateLinerError,
        LinerTooLargeError,
        LinerStorageError,
    ):
        assert issubclass(error, LinerError)
    assert issubclass(InvalidLinerValueError, ValueError)
    assert MAX_LINER_BYTES == 200 * 2**20
    assert MAX_LINER_NAME == 255
    assert (MIN_CADENCE, MAX_CADENCE) == (1, 1000)


def test_cadence_bounds() -> None:
    """LR1: any whole number 1-1,000; 0 and 1,001 are refused."""
    assert LinerCadence(1).songs == 1
    assert LinerCadence(1000).songs == 1000
    for songs in (0, 1001, -3):
        with pytest.raises(InvalidLinerValueError):
            LinerCadence(songs)


def test_cadence_from_setting_none_is_off() -> None:
    """LR1: a blank setting (NULL) means no liners; a number is that cadence."""
    assert LinerCadence.from_setting(None) is None
    assert LinerCadence.from_setting(3) == LinerCadence(3)


def test_liner_name_is_stripped_and_bounded() -> None:
    """LR3: the display name is 1-255 characters after trimming, and stored trimmed."""
    assert liner(name="  ID 1 ").name == "ID 1"
    assert liner(name=" " + "x" * 255 + " ").name == "x" * 255
    for bad in ("", "   ", "x" * 256):
        with pytest.raises(InvalidLinerValueError):
            liner(name=bad)


def test_liner_file_name_matches_id_and_format() -> None:
    """LR3 (plan note 4): the stored file is ``<liner id>.<format>``, nothing else."""
    assert liner(file_name=f"{LINER_ID}.mp3", format=ClipFormat.MP3).file_name.endswith(".mp3")
    assert LINER_FILE_NAME.fullmatch(f"{LINER_ID}.flac") is not None
    with pytest.raises(InvalidLinerValueError):
        liner(file_name=f"{LINER_ID}.wav", format=ClipFormat.MP3)
    with pytest.raises(InvalidLinerValueError):
        liner(file_name=f"{uuid4()}.wav")  # another liner's id
    with pytest.raises(InvalidLinerValueError):
        liner(file_name=f"../{LINER_ID}.wav")


def test_liner_duration_and_upload_time_are_checked() -> None:
    """LR2, LR3: a liner has some audio (duration > 0) and an aware upload time."""
    with pytest.raises(InvalidLinerValueError):
        liner(duration_ms=0)
    with pytest.raises(InvalidLinerValueError):
        liner(uploaded_at=datetime(2026, 10, 18, 12, 0))  # naive


def test_liner_markers_pin_fade_to_stop() -> None:
    """LR4, LR11 (plan note 7): a liner's Fade-out is always its Stop."""
    assert liner_markers(0, 5_000, 7_000) == CueMarkers(0, 5_000, 7_000, 7_000)


def test_liner_markers_past_duration_refused() -> None:
    """LR4, LR11: an edit ends within the file, and its fade sits at Stop."""
    assert liner(duration_ms=7_000, markers=liner_markers(0, 5_000, 7_000)).markers == (
        CueMarkers(0, 5_000, 7_000, 7_000)
    )
    with pytest.raises(InvalidLinerValueError):
        liner(duration_ms=6_000, markers=liner_markers(0, 5_000, 7_000))
    with pytest.raises(InvalidLinerValueError):
        liner(duration_ms=7_000, markers=CueMarkers(0, 5_000, 6_000, 7_000))


def test_points_no_row_play_whole_file() -> None:
    """LR4: before analysis the whole file plays, EOM at the end, no fade, unchanged level."""
    assert liner_points(7_000, None, None) == CuePoints(0, 7_000, 0, 0, 0, 0.0)


def test_points_autocue_row_loses_its_fade() -> None:
    """LR4: autocue's row plays with its gain and its EOM, but never with a fade."""
    assert liner_points(7_000, None, AUTO_ROW) == CuePoints(120, 6_900, 0, 0, 300, -3.5)


def test_points_failed_row_plays_whole_at_its_gain() -> None:
    """LR4: a failed analysis's fallback row plays, at its gain, with its fade removed."""
    assert liner_points(7_000, None, FAILED_ROW) == CuePoints(0, 7_000, 0, 0, 0, -8.0)


def test_points_edit_wins_with_row_gain() -> None:
    """LR4 (plan note 6): the manual edit's positions, autocue's gain, fade 0."""
    assert liner_points(7_000, EDIT, AUTO_ROW) == CuePoints(100, 6_500, 0, 0, 1_500, -3.5)
    # A failed (not silent) row still gives its gain to the edit.
    assert liner_points(7_000, EDIT, FAILED_ROW) == CuePoints(100, 6_500, 0, 0, 1_500, -8.0)


def test_points_edit_without_row_uses_no_cues_gain() -> None:
    """LR4 (plan note 6): an edit made before analysis plays at the no-cues gain."""
    assert liner_points(7_000, EDIT, None) == CuePoints(100, 6_500, 0, 0, 1_500, 0.0)


def test_points_silent_row_is_not_playable() -> None:
    """LR4: a silent liner never plays, edited or not."""
    assert liner_points(7_000, None, SILENT_ROW) is None
    assert liner_points(7_000, EDIT, SILENT_ROW) is None


def test_cue_state_order() -> None:
    """LR4, LR5: silent wins, then edited, then failed, then auto; no row is analysing."""
    assert {state.name: state.value for state in LinerCueState} == {
        "EDITED": "edited",
        "AUTO": "auto",
        "FAILED": "failed",
        "ANALYSING": "analysing",
        "SILENT": "silent",
    }
    assert liner_cue_state(EDIT, SILENT_ROW) is LinerCueState.SILENT
    assert liner_cue_state(None, SILENT_ROW) is LinerCueState.SILENT
    assert liner_cue_state(EDIT, FAILED_ROW) is LinerCueState.EDITED
    assert liner_cue_state(EDIT, AUTO_ROW) is LinerCueState.EDITED
    assert liner_cue_state(None, FAILED_ROW) is LinerCueState.FAILED
    assert liner_cue_state(None, AUTO_ROW) is LinerCueState.AUTO
    assert liner_cue_state(None, None) is LinerCueState.ANALYSING
    # Ambiguity resolved (report): an edit made before analysis is still an edit.
    assert liner_cue_state(EDIT, None) is LinerCueState.EDITED


def test_station_liner_cadence_defaults_off() -> None:
    """LR1, LR10 (plan note 3): a station built without a cadence has liners off."""
    assert BroadcastStation(uuid4(), "WKRP").liner_cadence is None
