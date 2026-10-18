"""The cue editor service (cue editor PR 2, Task 6; R3, R4, R6, Review Focus 3 and 4).

Requirements: R3 (a song opens on what the radio plays for it: autocue's values when
measured; the failed row's or the no-cues points otherwise, C2, C5); R4 (an edit is stored by
audio hash, twins share it, reset deletes it); R6 (Save refuses cues past the file's end and
cues the radio would refuse, C4); Review Focus 3 (a very short song opens, and Save refuses only
what D9 refuses, with a reason); Review Focus 4 (a row says present but the file is gone from
disk: not found, never a crash). Fakes only, no database.

Timing is ``StreamTiming()``: no fade-in, fade-out 4000, margin 3000 (PR #161), so D9's minimum
span is the fade-out plus 3000.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.domain.enums import FileStatus
from backend.domain.library import AudioHash, AudioMetadata, LibraryFile
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CueAudioUnreadableError,
    CueEditFileNotFoundError,
    CueEditRejectedError,
    CueFileNotEditableError,
    CueMarkers,
    CuePoints,
    StreamTiming,
)
from backend.playout.peaks import PeakReadError, Peaks
from backend.services.streaming.cue_editor import (
    AudioSource,
    CueAnalysisState,
    CueEditorPorts,
    CueEditorView,
    Waveform,
    audio_path,
    read_cue_editor,
    read_waveform,
    reset_cue_edit,
    save_cue_edit,
)
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.stream_cue_edits import FakeStreamCueEditRepository
from tests.fakes.stream_cues import FakeStreamCueRepository

TIMING = StreamTiming()
FILE_ID = UUID("00000000-0000-0000-0000-0000000000a1")
HASH = AudioHash.parse("flac-md5:" + "a" * 32)
DURATION_MS = 252_893
HERETIC = CuePoints(
    cue_in_ms=0,
    cue_out_ms=247_100,
    fade_in_ms=0,
    fade_out_ms=6_000,
    start_next_ms=6_000,
    gain_db=-11.63,
)
HERETIC_OPENS_AS = CueMarkers(start_ms=0, eom_ms=241_100, fade_at_ms=241_100, stop_ms=247_100)
# What the radio plays for a song with no real analysis: the whole file, a 4 s fade, no overlap.
WHOLE_FILE = CueMarkers(start_ms=0, eom_ms=252_893, fade_at_ms=248_893, stop_ms=252_893)
EDIT = CueMarkers(start_ms=9_000, eom_ms=241_100, fade_at_ms=244_000, stop_ms=247_100)
NOT_FINGERPRINTED = "This file hasn't been fingerprinted yet, so its cues can't be edited."
NO_LENGTH = "This file has no known length."
SILENT_REFUSAL = "This song was measured silent, so the radio skips it; its cues can't be edited."


@dataclass
class Rig:
    """The three fakes behind one ``CueEditorPorts``, and the file under test."""

    files: FakeLibraryFileRepository
    cues: FakeStreamCueRepository
    edits: FakeStreamCueEditRepository
    ports: CueEditorPorts
    path: Path

    def add_file(self, *, file_status: FileStatus = FileStatus.PRESENT, **changes: Any) -> None:
        """Store the Heretic Anthem FLAC with some fields changed. The fake's upsert marks a
        row present, so a missing file is marked missing afterwards, as a rescan would."""
        self.files.upsert(dataclasses.replace(heretic(self.path), **changes))
        if file_status == FileStatus.MISSING:
            self.files.mark_missing(str(self.path))

    def measure(self, points: CuePoints = HERETIC, *, failed: bool = False) -> None:
        self.cues.upsert(
            CueAnalysis(HASH, points, None if failed else -14.0, failed, CUE_ANALYSER_VERSION)
        )

    def measure_silent(self, duration_ms: int = DURATION_MS) -> None:
        """A D111 row: the analysis found no audio; fallback values, failed and silent."""
        fallback = CuePoints(0, duration_ms, 0, 4_000, 0, -8.0)
        self.cues.upsert(
            CueAnalysis(HASH, fallback, None, True, CUE_ANALYSER_VERSION, audio_silent=True)
        )


def heretic(path: Path, *, duration_ms: int | None = DURATION_MS) -> LibraryFile:
    return LibraryFile(
        id=FILE_ID,
        file_path=str(path),
        format="flac",
        file_status=FileStatus.PRESENT,
        audio_hash=HASH,
        audio=AudioMetadata(
            track_title="The Heretic Anthem", artist_name="Slipknot", duration_ms=duration_ms
        ),
    )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """A present, fingerprinted FLAC whose path is NOT on disk (only audio and waveform look)."""
    files = FakeLibraryFileRepository()
    cues = FakeStreamCueRepository()
    edits = FakeStreamCueEditRepository()
    built = Rig(files, cues, edits, CueEditorPorts(files, cues, edits), tmp_path / "heretic.flac")
    built.add_file()
    return built


def on_disk(rig: Rig) -> Path:
    rig.path.write_bytes(b"fLaC")
    return rig.path


def refusal(call: Callable[[], object]) -> CueEditRejectedError:
    with pytest.raises(CueEditRejectedError) as raised:
        call()
    return raised.value


# --- The view: what a song opens on (R3, C2, C5) ------------------------------------------


def test_view_of_measured_song_opens_on_autocue(rig: Rig) -> None:
    """R3, C2: a measured song opens on autocue's values: EOM and fade at its fade start."""
    rig.measure()
    view = read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert view == CueEditorView(
        file_id=FILE_ID,
        title="The Heretic Anthem",
        artist="Slipknot",
        format="flac",
        duration_ms=252_893,
        analysis=CueAnalysisState.MEASURED,
        automatic=HERETIC_OPENS_AS,
        manual=None,
    )


def test_view_of_failed_song_opens_on_its_fallback_row(rig: Rig) -> None:
    """R3, C5: an analysis_failed row (D52 fallback) opens as that row: the whole file."""
    fallback = CuePoints(0, 252_893, 0, 4_000, 0, -8.0)
    rig.measure(fallback, failed=True)
    view = read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert view.automatic == CueMarkers(0, 252_893, 248_893, 252_893)
    assert view.analysis == CueAnalysisState.FAILED
    assert view.manual is None


def test_view_of_unmeasured_song_opens_on_d23(rig: Rig) -> None:
    """R3, C5: no row at all opens on D23: start 0, stop at the end, a 4 s fade, EOM at the end."""
    view = read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert view.automatic == WHOLE_FILE
    assert view.analysis == CueAnalysisState.WAITING
    assert view.manual is None


def test_view_of_silent_song_says_silent(rig: Rig) -> None:
    """R3, C2, D111: a row measured silent opens on its whole-file fallback markers, flagged
    SILENT (not 'failed': silence is not a failure), so the flat waveform can be shown."""
    rig.measure_silent()
    view = read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert view.analysis == CueAnalysisState.SILENT
    assert view.automatic == CueMarkers(0, 252_893, 248_893, 252_893)
    assert view.manual is None


def test_view_duration_is_the_longer_of_the_tag_and_autocues_stop(rig: Rig) -> None:
    """R3, R6, C2: when the tagged length (250_000) is shorter than the measured Stop
    (252_893), the editor's length is the Stop, so the opening markers are always savable."""
    rig.add_file(audio=AudioMetadata(track_title="Retagged", duration_ms=250_000))
    rig.measure(CuePoints(0, 252_893, 0, 6_000, 6_000, -11.63))
    view = read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert view.duration_ms == 252_893
    assert view.automatic == CueMarkers(0, 246_893, 246_893, 252_893)

    save_cue_edit(rig.ports, FILE_ID, view.automatic, TIMING)
    assert rig.edits.edits == {HASH: view.automatic}


def test_view_duration_is_the_tag_when_autocue_stops_earlier(rig: Rig) -> None:
    """R3: the usual case (Stop before the end of the file) keeps the tagged length."""
    rig.measure()
    assert read_cue_editor(rig.ports, FILE_ID, TIMING).duration_ms == 252_893


def test_view_shows_a_saved_edit_as_manual(rig: Rig) -> None:
    """R3, R4: a stored edit is the manual markers; the automatic ones stay autocue's."""
    rig.measure()
    rig.edits.save(HASH, EDIT)
    view = read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert view.manual == EDIT
    assert view.automatic == HERETIC_OPENS_AS
    assert view.analysis == CueAnalysisState.MEASURED


def test_view_holds_a_fade_longer_than_the_song_inside_it(rig: Rig) -> None:
    """Review Focus 3: a stored fade longer than the span still opens, with the fade at Start."""
    odd = CuePoints(0, 5_000, 0, 9_000, 0, -8.0)
    rig.measure(odd, failed=True)
    view = read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert view.automatic == CueMarkers(start_ms=0, eom_ms=5_000, fade_at_ms=0, stop_ms=5_000)


def test_view_with_missing_tags_leaves_title_and_artist_empty(rig: Rig) -> None:
    """R3: an untagged file opens with no title or artist (None), not an error."""
    rig.add_file(audio=AudioMetadata(duration_ms=DURATION_MS))
    view = read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert (view.title, view.artist) == (None, None)


def test_viewing_writes_nothing(rig: Rig) -> None:
    """CQRS (design-pattern signals): a query changes no repository."""
    rig.measure()
    before = dict(rig.cues.analyses)
    read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert rig.edits.edits == {}
    assert rig.cues.analyses == before


def test_the_analysis_states_are_the_words_the_api_sends() -> None:
    """Interfaces: CueAnalysisState values are lowercase strings (they go out as JSON)."""
    assert [state.value for state in CueAnalysisState] == [
        "measured",
        "failed",
        "waiting",
        "silent",
    ]
    assert CueAnalysisState.MEASURED == "measured"


def test_the_service_results_are_frozen() -> None:
    """Project rules: results are frozen dataclasses."""
    for value, field in (
        (Waveform(1, 200, b""), "duration_ms"),
        (AudioSource("a.flac", "flac"), "path"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, field, 2)


# --- Save (R4, R6, C4) -------------------------------------------------------------------


def test_save_stores_by_audio_hash(rig: Rig) -> None:
    """R4: the edit is stored under the file's audio hash; a command returns nothing."""
    assert save_cue_edit(rig.ports, FILE_ID, EDIT, TIMING) is None
    assert rig.edits.edits == {HASH: EDIT}


def test_a_twin_file_shares_the_edit(rig: Rig) -> None:
    """R4, Review Focus 1: an identical copy (same audio hash) opens on the same edit; a file
    with different audio does not, and the edit is stored once."""
    twin = uuid4()
    other = uuid4()
    rig.files.upsert(dataclasses.replace(heretic(rig.path.with_name("copy.flac")), id=twin))
    rig.files.upsert(
        dataclasses.replace(
            heretic(rig.path.with_name("other.flac")),
            id=other,
            audio_hash=AudioHash.parse("flac-md5:" + "b" * 32),
        )
    )
    save_cue_edit(rig.ports, FILE_ID, EDIT, TIMING)
    assert read_cue_editor(rig.ports, twin, TIMING).manual == EDIT
    assert read_cue_editor(rig.ports, other, TIMING).manual is None
    assert list(rig.edits.edits) == [HASH]


def test_save_replaces_an_earlier_edit(rig: Rig) -> None:
    """R4: saving again replaces the edit; there is one per audio."""
    later = CueMarkers(start_ms=1_000, eom_ms=200_000, fade_at_ms=200_000, stop_ms=210_000)
    save_cue_edit(rig.ports, FILE_ID, EDIT, TIMING)
    save_cue_edit(rig.ports, FILE_ID, later, TIMING)
    assert rig.edits.edits == {HASH: later}


def test_save_refused_past_the_end_of_file(rig: Rig) -> None:
    """R6: Stop one ms past the file's 252_893 ms is refused, with a reason, and not stored."""
    past = CueMarkers(start_ms=0, eom_ms=252_894, fade_at_ms=248_894, stop_ms=252_894)
    error = refusal(lambda: save_cue_edit(rig.ports, FILE_ID, past, TIMING))
    assert str(error).strip()
    assert rig.edits.edits == {}


def test_save_accepts_stop_exactly_at_the_end_of_file(rig: Rig) -> None:
    """R6: Stop equal to the file's length is allowed (stop <= duration)."""
    save_cue_edit(rig.ports, FILE_ID, WHOLE_FILE, TIMING)
    assert rig.edits.edits == {HASH: WHOLE_FILE}


def test_save_refused_when_fade_leaves_no_margin(rig: Rig) -> None:
    """R6, C4, Review Focus 3: an 8 s fade needs 11 s of song but the span is 10 s; the reason
    names the 3 s rule and nothing is stored."""
    squeezed = CueMarkers(start_ms=0, eom_ms=9_000, fade_at_ms=2_000, stop_ms=10_000)
    error = refusal(lambda: save_cue_edit(rig.ports, FILE_ID, squeezed, TIMING))
    assert "3 s" in str(error)
    assert rig.edits.edits == {}


def test_save_refusal_counts_the_span_from_start_not_from_zero(rig: Rig) -> None:
    """R6, C4: D9 measures the span between Start and Stop: from 5 s to 15 s is 10 s, so an 8 s
    fade is refused even though Stop is 15 s into the file."""
    squeezed = CueMarkers(start_ms=5_000, eom_ms=14_000, fade_at_ms=7_000, stop_ms=15_000)
    assert "3 s" in str(refusal(lambda: save_cue_edit(rig.ports, FILE_ID, squeezed, TIMING)))


def test_save_accepts_a_fade_that_leaves_exactly_three_seconds(rig: Rig) -> None:
    """R6, C4: fade 7 s + 3 s margin = the 10 s span, which D9 allows (span >= fade + margin)."""
    exact = CueMarkers(start_ms=0, eom_ms=9_000, fade_at_ms=3_000, stop_ms=10_000)
    save_cue_edit(rig.ports, FILE_ID, exact, TIMING)
    assert rig.edits.edits == {HASH: exact}


def test_save_refuses_a_fade_one_millisecond_too_long(rig: Rig) -> None:
    """R6, C4: fade 7.001 s + 3 s margin > the 10 s span: refused."""
    over = CueMarkers(start_ms=0, eom_ms=9_000, fade_at_ms=2_999, stop_ms=10_000)
    refusal(lambda: save_cue_edit(rig.ports, FILE_ID, over, TIMING))
    assert rig.edits.edits == {}


def test_the_rule_follows_the_timing_it_is_given(rig: Rig) -> None:
    """C4: Save asks the domain's own is_playable(timing); a 5 s margin refuses what 3 s allows."""
    exact = CueMarkers(start_ms=0, eom_ms=9_000, fade_at_ms=3_000, stop_ms=10_000)
    strict = StreamTiming(margin_ms=5_000)
    refusal(lambda: save_cue_edit(rig.ports, FILE_ID, exact, strict))
    assert rig.edits.edits == {}


def test_save_checks_nothing_but_the_end_and_the_radios_rule(rig: Rig) -> None:
    """R1, R6: EOM and the fade may sit in either order; the service does not reorder them."""
    swapped = CueMarkers(start_ms=0, eom_ms=100_000, fade_at_ms=200_000, stop_ms=210_000)
    save_cue_edit(rig.ports, FILE_ID, swapped, TIMING)
    assert rig.edits.edits == {HASH: swapped}


def test_short_song_opens_and_refuses_save(rig: Rig) -> None:
    """Review Focus 3: a 6 s song with no row opens; Save refuses the 4 s fade (4 + 3 > 6) and
    keeps the no-fade version (0 + 3 <= 6)."""
    rig.add_file(audio=AudioMetadata(track_title="Short", duration_ms=6_000))
    view = read_cue_editor(rig.ports, FILE_ID, TIMING)
    assert view.automatic == CueMarkers(start_ms=0, eom_ms=6_000, fade_at_ms=2_000, stop_ms=6_000)
    assert view.duration_ms == 6_000

    fading = CueMarkers(start_ms=0, eom_ms=6_000, fade_at_ms=2_000, stop_ms=6_000)
    refusal(lambda: save_cue_edit(rig.ports, FILE_ID, fading, TIMING))
    assert rig.edits.edits == {}

    hard_stop = CueMarkers(start_ms=0, eom_ms=6_000, fade_at_ms=6_000, stop_ms=6_000)
    save_cue_edit(rig.ports, FILE_ID, hard_stop, TIMING)
    assert rig.edits.edits == {HASH: hard_stop}


def test_a_refused_edit_leaves_autocue_alone(rig: Rig) -> None:
    """R6, C4: with a measured row present the verdict is the same (the edit's own points
    decide), and the measured row is untouched."""
    rig.measure()
    squeezed = CueMarkers(start_ms=0, eom_ms=9_000, fade_at_ms=2_000, stop_ms=10_000)
    refusal(lambda: save_cue_edit(rig.ports, FILE_ID, squeezed, TIMING))
    assert rig.edits.edits == {}
    assert rig.cues.analyses[HASH].cues == HERETIC


def test_save_refused_for_silent_audio(rig: Rig) -> None:
    """C4, anti-goal 2, D111: the radio gives silent audio no file, edit or not, so an edit
    would be one the radio never plays: refused, with this sentence, and nothing stored."""
    rig.measure_silent()
    error = refusal(lambda: save_cue_edit(rig.ports, FILE_ID, EDIT, TIMING))
    assert str(error) == SILENT_REFUSAL
    assert rig.edits.edits == {}


# --- Reset (R4) ---------------------------------------------------------------------------


def test_reset_deletes_the_edit(rig: Rig) -> None:
    """R4: after reset the view shows no manual edit, autocue's row is untouched."""
    rig.measure()
    save_cue_edit(rig.ports, FILE_ID, EDIT, TIMING)
    assert reset_cue_edit(rig.ports, FILE_ID) is None
    assert read_cue_editor(rig.ports, FILE_ID, TIMING).manual is None
    assert rig.edits.edits == {}
    assert rig.cues.analyses[HASH].cues == HERETIC


def test_reset_without_edit_is_fine(rig: Rig) -> None:
    """R4: resetting a song that has no edit is not an error."""
    reset_cue_edit(rig.ports, FILE_ID)
    assert rig.edits.edits == {}


def test_reset_still_works_on_silent_audio(rig: Rig) -> None:
    """R4, D111: an edit left from before the audio was measured silent can still be removed."""
    rig.edits.save(HASH, EDIT)
    rig.measure_silent()
    reset_cue_edit(rig.ports, FILE_ID)
    assert rig.edits.edits == {}


def test_reset_leaves_other_audios_edits(rig: Rig) -> None:
    """R4: reset deletes this audio's edit and no other."""
    elsewhere = AudioHash.parse("flac-md5:" + "c" * 32)
    rig.edits.save(elsewhere, EDIT)
    rig.edits.save(HASH, EDIT)
    reset_cue_edit(rig.ports, FILE_ID)
    assert rig.edits.edits == {elsewhere: EDIT}


# --- Files that cannot be edited (R3, R4, Review Focus 4) ----------------------------------

ALL_CALLS: list[tuple[str, Callable[[Rig, UUID], object]]] = [
    ("view", lambda r, i: read_cue_editor(r.ports, i, TIMING)),
    ("save", lambda r, i: save_cue_edit(r.ports, i, EDIT, TIMING)),
    ("reset", lambda r, i: reset_cue_edit(r.ports, i)),
    ("waveform", lambda r, i: read_waveform(r.ports, i, lambda path: Peaks(200, b"\x01"))),
    ("audio", lambda r, i: audio_path(r.ports, i)),
]
CALL_IDS = [name for name, _ in ALL_CALLS]
CALLS = [call for _, call in ALL_CALLS]


@pytest.mark.parametrize("call", CALLS, ids=CALL_IDS)
def test_unknown_file_is_not_found(rig: Rig, call: Callable[[Rig, UUID], object]) -> None:
    """R7 via the service: every operation first resolves the file; no row is not found."""
    with pytest.raises(CueEditFileNotFoundError):
        call(rig, uuid4())


@pytest.mark.parametrize("call", CALLS, ids=CALL_IDS)
def test_missing_file_is_not_found(rig: Rig, call: Callable[[Rig, UUID], object]) -> None:
    """R7 via the service: a row whose file_status is missing is not found, even when it is
    also unhashed (the file is resolved before anything else is asked of it)."""
    on_disk(rig)
    rig.add_file(file_status=FileStatus.MISSING, audio_hash=None)
    with pytest.raises(CueEditFileNotFoundError):
        call(rig, FILE_ID)


@pytest.mark.parametrize(
    "call",
    [call for name, call in ALL_CALLS if name in {"view", "save", "reset"}],
    ids=["view", "save", "reset"],
)
def test_unhashed_file_is_not_editable(rig: Rig, call: Callable[[Rig, UUID], object]) -> None:
    """R4: with no audio hash an edit has nothing to be stored under: 409 material, with the
    sentence the page shows."""
    rig.add_file(audio_hash=None)
    with pytest.raises(CueFileNotEditableError) as raised:
        call(rig, FILE_ID)
    assert str(raised.value) == NOT_FINGERPRINTED
    assert rig.edits.edits == {}


@pytest.mark.parametrize("duration_ms", [None, 0])
@pytest.mark.parametrize(
    "call",
    [call for name, call in ALL_CALLS if name in {"view", "save", "reset"}],
    ids=["view", "save", "reset"],
)
def test_file_without_a_length_is_not_editable(
    rig: Rig, call: Callable[[Rig, UUID], object], duration_ms: int | None
) -> None:
    """R6: with no known length there is nothing to validate a Stop against (and no D23
    points to open on): not editable, with its own sentence."""
    rig.add_file(audio=AudioMetadata(track_title="Mystery", duration_ms=duration_ms))
    with pytest.raises(CueFileNotEditableError) as raised:
        call(rig, FILE_ID)
    assert str(raised.value) == NO_LENGTH
    assert rig.edits.edits == {}


# --- Waveform and audio (R7, Review Focus 4) ----------------------------------------------


def test_waveform_uses_the_reader_and_duration(rig: Rig) -> None:
    """R7: the waveform is the reader's peaks for the file's path, with the file's length."""
    on_disk(rig)
    asked: list[str] = []

    def read(path: str) -> Peaks:
        asked.append(path)
        return Peaks(200, b"\x01\x02")

    waveform = read_waveform(rig.ports, FILE_ID, read)
    assert waveform == Waveform(duration_ms=252_893, per_second=200, peaks=b"\x01\x02")
    assert asked == [str(rig.path)]


def test_waveform_reader_failure_is_unreadable(rig: Rig) -> None:
    """R7: a PeakReadError from the reader becomes the domain's CueAudioUnreadableError."""
    on_disk(rig)

    def read(path: str) -> Peaks:
        raise PeakReadError("Invalid data found when processing input")

    with pytest.raises(CueAudioUnreadableError):
        read_waveform(rig.ports, FILE_ID, read)


def test_audio_file_gone_from_disk_is_not_found(rig: Rig) -> None:
    """Review Focus 4: the row says present but the path is gone: not found, and the reader is
    never asked to decode a file that is not there."""
    assert not rig.path.exists()

    def read(path: str) -> Peaks:
        raise AssertionError(f"the reader must not run for a file that is gone ({path})")

    with pytest.raises(CueEditFileNotFoundError):
        read_waveform(rig.ports, FILE_ID, read)
    with pytest.raises(CueEditFileNotFoundError):
        audio_path(rig.ports, FILE_ID)


def test_a_directory_in_place_of_the_file_is_not_found(rig: Rig) -> None:
    """Review Focus 4: the check is is_file(), so a folder at the path is not audio either."""
    rig.path.mkdir()
    with pytest.raises(CueEditFileNotFoundError):
        audio_path(rig.ports, FILE_ID)


def test_waveform_and_audio_work_for_a_present_file_without_a_hash(rig: Rig) -> None:
    """R7 (ruling): only view, save and reset need a hash; the audio and its waveform are
    served for any present file."""
    on_disk(rig)
    rig.add_file(audio_hash=None)
    waveform = read_waveform(rig.ports, FILE_ID, lambda path: Peaks(200, b"\x05"))
    assert waveform.peaks == b"\x05"
    assert audio_path(rig.ports, FILE_ID) == AudioSource(str(rig.path), "flac")


def test_audio_works_for_a_present_file_with_no_hash_and_no_length(rig: Rig) -> None:
    """R7 (ruling): not even the length is needed to serve the audio."""
    on_disk(rig)
    rig.add_file(audio_hash=None, audio=AudioMetadata(duration_ms=None))
    assert audio_path(rig.ports, FILE_ID) == AudioSource(str(rig.path), "flac")


def test_audio_path_of_present_file(rig: Rig) -> None:
    """R7: the audio's path and format, for the route to serve."""
    on_disk(rig)
    assert audio_path(rig.ports, FILE_ID) == AudioSource(str(rig.path), "flac")
