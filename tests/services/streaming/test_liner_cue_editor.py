"""Station liners, PR L3 Task 14: the liner cue editor service,
``backend/services/streaming/liner_cues.py`` (fakes plus ``tmp_path``).

Requirements: LR11 (the cue editor opens a liner by its id, with Fade-out pinned to Stop; Save
checks the liner rule, span > 0 and EOM within the span, and Stop <= the liner's length),
LR4 (manual edits are per liner, never by audio hash; a silent liner never plays; no D9 minimum
length), LR3 (each liner belongs to one station: another station's liner is not found).

The service reuses the song editor's ``CueEditorView``, ``CueAnalysisState`` and ``Waveform``.
A liner opens on what the radio plays for it (``liner_points``: autocue's row with no fades,
or the whole file before analysis). The edit is stored on the liner row through
``StationLinerRepository.save_markers`` and committed through ``LinerCuePorts.commit``;
``stream_cue_edits`` is not one of the service's ports, so it cannot be written.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.domain.library import AudioHash
from backend.domain.liners import LinerNotFoundError, StationLiner
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    ClipFormat,
    CueAnalysis,
    CueAudioUnreadableError,
    CueEditRejectedError,
    CueMarkers,
    CuePoints,
    InvalidStreamValueError,
)
from backend.playout.peaks import PeakReadError, Peaks
from backend.services.streaming.cue_editor import CueAnalysisState, CueEditorView, Waveform
from backend.services.streaming.liner_cues import (
    LinerCuePorts,
    read_liner_cues,
    read_liner_waveform,
    reset_liner_cues,
    save_liner_cues,
)
from tests.fakes.station_liners import FakeStationLiners
from tests.fakes.stream_cues import FakeStreamCueRepository

STATION_ID = UUID("0a1b2c3d-4e5f-4a6b-9c7d-8e9fa0b1c2d3")
OTHER_STATION_ID = UUID("1b2c3d4e-5f60-4b7c-8d9e-0fa1b2c3d4e5")
LINER_ID = UUID("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab")
TWIN_ID = UUID("7a2d3e4f-5b6c-4d7e-9f80-123456789abc")
HASH = AudioHash.parse("audio-sha256:" + "a" * 64)
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
DURATION_MS = 7_000
# Autocue's row as the brief gives it: (cue_in, cue_out, fade_in, fade_out, start_next, gain).
MEASURED = CuePoints(120, 6_900, 0, 400, 300, -3.5)
# A liner has no fade, so EOM is Stop - start_next and Fade-out sits at Stop.
MEASURED_OPENS_AS = CueMarkers(start_ms=120, eom_ms=6_600, fade_at_ms=6_900, stop_ms=6_900)
WHOLE_FILE = CueMarkers(start_ms=0, eom_ms=DURATION_MS, fade_at_ms=DURATION_MS, stop_ms=DURATION_MS)
EDIT = CueMarkers(start_ms=500, eom_ms=6_000, fade_at_ms=6_800, stop_ms=6_800)
SILENT_REFUSAL = "This liner was measured silent, so the radio skips it; its cues can't be edited."
PAST_THE_END = "Stop can't be past the end of the liner (0:07.000)."


def station_liner(
    liner_id: UUID = LINER_ID,
    *,
    station_id: UUID = STATION_ID,
    name: str = "Station ID",
    fmt: ClipFormat = ClipFormat.MP3,
    duration_ms: int = DURATION_MS,
    markers: CueMarkers | None = None,
    audio_hash: AudioHash = HASH,
) -> StationLiner:
    return StationLiner(
        id=liner_id,
        station_id=station_id,
        name=name,
        file_name=f"{liner_id}.{fmt.value}",
        format=fmt,
        audio_hash=audio_hash,
        duration_ms=duration_ms,
        markers=markers,
        uploaded_at=NOW,
    )


@dataclass
class Rig:
    """The liner fake over the cue fake, a liners root under ``tmp_path``, and a commit that
    records the liner's stored edit at the moment it is called (so a commit before the write
    shows the old edit)."""

    root: Path
    cues: FakeStreamCueRepository = field(default_factory=FakeStreamCueRepository)
    commits: list[CueMarkers | None] = field(default_factory=list)
    liners: FakeStationLiners = field(init=False)

    def __post_init__(self) -> None:
        self.liners = FakeStationLiners(self.cues)

    @property
    def ports(self) -> LinerCuePorts:
        return LinerCuePorts(
            liners=self.liners, cues=self.cues, folder=self.root, commit=self.record_commit
        )

    def record_commit(self) -> None:
        stored = self.liners.get(LINER_ID)
        self.commits.append(None if stored is None else stored.markers)

    def add(self, liner: StationLiner | None = None) -> StationLiner:
        liner = liner if liner is not None else station_liner()
        self.liners.add(liner)
        return liner

    def measure(self, points: CuePoints = MEASURED, *, failed: bool = False) -> None:
        self.cues.upsert(
            CueAnalysis(HASH, points, None if failed else -14.0, failed, CUE_ANALYSER_VERSION)
        )

    def measure_silent(self) -> None:
        """A D111 row: fallback values that are NOT the whole file, so a view that opened on
        the row instead of the whole file would show."""
        fallback = CuePoints(0, 6_500, 0, 4_000, 0, -8.0)
        self.cues.upsert(
            CueAnalysis(HASH, fallback, None, True, CUE_ANALYSER_VERSION, audio_silent=True)
        )

    def stored(self, liner_id: UUID = LINER_ID) -> CueMarkers | None:
        liner = self.liners.get(liner_id)
        assert liner is not None
        return liner.markers

    def put_on_disk(self, liner: StationLiner, content: bytes = b"ID3audio") -> Path:
        folder = self.root / str(liner.station_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / liner.file_name
        path.write_bytes(content)
        return path


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    built = Rig(tmp_path / "work" / "liners")
    built.add()
    return built


def never_read(path: str) -> Peaks:
    raise AssertionError(f"the reader must not run ({path})")


# --- The ports -----------------------------------------------------------------------------


def test_the_ports_are_the_liners_the_cues_the_folder_and_a_commit() -> None:
    """LR4, LR11: the service is wired to exactly these; it has no ``stream_cue_edits`` port,
    so a liner edit can never be stored by audio hash. Frozen, like every port bundle."""
    assert {f.name for f in dataclasses.fields(LinerCuePorts)} == {
        "liners",
        "cues",
        "folder",
        "commit",
    }
    ports = LinerCuePorts(
        liners=FakeStationLiners(),
        cues=FakeStreamCueRepository(),
        folder=Path("x"),
        commit=lambda: None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ports.folder = Path("y")  # type: ignore[misc]


# --- The view (LR11, LR4) ------------------------------------------------------------------


def test_view_opens_on_autocue_with_fade_at_stop(rig: Rig) -> None:
    """LR11, LR4: a measured liner opens on autocue's row as the radio plays it, with no
    fade: EOM is Stop - start_next (6_600) and Fade-out sits at Stop (6_900). Viewing writes
    nothing and commits nothing."""
    rig.measure()
    view = read_liner_cues(rig.ports, STATION_ID, LINER_ID)
    assert view == CueEditorView(
        file_id=LINER_ID,
        title="Station ID",
        artist=None,
        format="mp3",
        duration_ms=DURATION_MS,
        analysis=CueAnalysisState.MEASURED,
        automatic=MEASURED_OPENS_AS,
        manual=None,
    )
    assert rig.commits == []
    assert rig.stored() is None


def test_view_of_failed_liner_opens_on_its_row_without_a_fade(rig: Rig) -> None:
    """LR11, LR4: an analysis_failed row says FAILED and still opens on that row, fade-free
    (its 4 s fade-out is dropped: Fade-out at Stop)."""
    rig.measure(CuePoints(0, 6_000, 0, 4_000, 0, -8.0), failed=True)
    view = read_liner_cues(rig.ports, STATION_ID, LINER_ID)
    assert view.analysis == CueAnalysisState.FAILED
    assert view.automatic == CueMarkers(0, 6_000, 6_000, 6_000)


def test_view_of_unanalysed_liner_is_whole_file(rig: Rig) -> None:
    """LR4, LR11: with no ``stream_cues`` row the liner plays the whole file, EOM at the very
    end, no fade: it opens on (0, d, d, d), WAITING."""
    view = read_liner_cues(rig.ports, STATION_ID, LINER_ID)
    assert view.automatic == WHOLE_FILE
    assert view.analysis == CueAnalysisState.WAITING
    assert view.manual is None


def test_view_of_silent_liner(rig: Rig) -> None:
    """LR4, LR11, D111: a liner measured silent says SILENT and opens on the whole file
    (0, d, d, d), not on the silent row's fallback values."""
    rig.measure_silent()
    view = read_liner_cues(rig.ports, STATION_ID, LINER_ID)
    assert view.analysis == CueAnalysisState.SILENT
    assert view.automatic == WHOLE_FILE


def test_view_clamps_stop_to_the_liners_length(rig: Rig) -> None:
    """LR11: autocue's Stop (7_400) past the liner's stored length (7_000) is clamped to the
    length, Fade-out with it, so the markers the liner opens on can always be saved."""
    rig.measure(CuePoints(120, 7_400, 0, 400, 900, -3.5))
    view = read_liner_cues(rig.ports, STATION_ID, LINER_ID)
    assert view.duration_ms == DURATION_MS
    assert view.automatic == CueMarkers(start_ms=120, eom_ms=6_500, fade_at_ms=7_000, stop_ms=7_000)

    save_liner_cues(rig.ports, STATION_ID, LINER_ID, 120, 6_500, 7_000)
    assert rig.stored() == view.automatic


def test_view_opens_and_saves_when_autocues_eom_is_past_the_length_too(rig: Rig) -> None:
    """LR11 (audit M2): autocue's EOM (7_300) is past the liner's 7_000 ms as well as its Stop.
    The liner still opens, on markers that can be saved: Stop and Fade-out at the length, and
    EOM after Start and not past the length. Which EOM it opens on is the builder's call."""
    rig.measure(CuePoints(120, 7_400, 0, 400, 100, -3.5))
    opened = read_liner_cues(rig.ports, STATION_ID, LINER_ID).automatic
    assert opened.stop_ms == opened.fade_at_ms == DURATION_MS
    assert opened.start_ms < opened.eom_ms <= DURATION_MS

    save_liner_cues(rig.ports, STATION_ID, LINER_ID, opened.start_ms, opened.eom_ms, opened.stop_ms)
    assert rig.stored() == opened


def test_view_shows_the_saved_edit_as_manual(rig: Rig) -> None:
    """LR11, LR4: the liner's own stored edit is the manual markers; automatic stays
    autocue's."""
    rig.liners.save_markers(LINER_ID, EDIT)
    rig.measure()
    view = read_liner_cues(rig.ports, STATION_ID, LINER_ID)
    assert view.manual == EDIT
    assert view.automatic == MEASURED_OPENS_AS
    assert view.analysis == CueAnalysisState.MEASURED


def test_view_names_the_liner(rig: Rig) -> None:
    """LR11: the editor's title is the liner's display name, there is no artist, the format
    is the liner's, and ``file_id`` is the liner's id (the editor opens a liner by its id)."""
    rig.liners.rename(LINER_ID, "Top of the Hour")
    view = read_liner_cues(rig.ports, STATION_ID, LINER_ID)
    assert view.title == "Top of the Hour"
    assert view.artist is None
    assert view.format == "mp3"
    assert view.file_id == LINER_ID
    assert view.duration_ms == DURATION_MS


def test_view_reports_a_wav_liners_format(rig: Rig) -> None:
    """LR11: the format is the liner's own (the format value, not a fixed string)."""
    other_audio = AudioHash.parse("audio-sha256:" + "b" * 64)
    wav = rig.add(station_liner(uuid4(), fmt=ClipFormat.WAV, audio_hash=other_audio))
    assert read_liner_cues(rig.ports, STATION_ID, wav.id).format == "wav"


# --- Save (LR11, LR4) ----------------------------------------------------------------------


def test_save_stores_on_the_liner_not_by_hash(rig: Rig) -> None:
    """LR4, LR11: the edit is stored on this liner, Fade-out at Stop, and committed after it is
    stored. Another station's liner with the same audio keeps no edit, and autocue's shared row
    is untouched. A command returns nothing."""
    rig.add(station_liner(TWIN_ID, station_id=OTHER_STATION_ID))
    rig.measure()
    before = dict(rig.cues.analyses)

    assert save_liner_cues(rig.ports, STATION_ID, LINER_ID, 500, 6_000, 6_800) is None

    assert rig.stored() == CueMarkers(start_ms=500, eom_ms=6_000, fade_at_ms=6_800, stop_ms=6_800)
    assert rig.stored(TWIN_ID) is None
    assert rig.cues.analyses == before
    assert rig.commits == [EDIT]


def test_save_replaces_an_earlier_edit(rig: Rig) -> None:
    """LR4: saving again replaces the liner's edit."""
    save_liner_cues(rig.ports, STATION_ID, LINER_ID, 500, 6_000, 6_800)
    save_liner_cues(rig.ports, STATION_ID, LINER_ID, 0, 3_000, 4_000)
    assert rig.stored() == CueMarkers(0, 3_000, 4_000, 4_000)
    assert len(rig.commits) == 2


def test_save_accepts_stop_exactly_at_the_end(rig: Rig) -> None:
    """LR11: Stop equal to the liner's length is allowed (stop <= duration)."""
    save_liner_cues(rig.ports, STATION_ID, LINER_ID, 0, DURATION_MS, DURATION_MS)
    assert rig.stored() == WHOLE_FILE


def test_save_past_end_refused(rig: Rig) -> None:
    """LR11: Stop one ms past the liner's 7_000 ms is refused with the sentence naming the end
    as m:ss.mmm; nothing is stored or committed."""
    with pytest.raises(CueEditRejectedError) as raised:
        save_liner_cues(rig.ports, STATION_ID, LINER_ID, 0, 6_000, DURATION_MS + 1)
    assert str(raised.value) == PAST_THE_END
    assert rig.stored() is None
    assert rig.commits == []


@pytest.mark.parametrize(
    ("start_ms", "eom_ms", "stop_ms"),
    [(1_000, 1_000, 6_000), (1_000, 900, 6_000), (1_000, 6_500, 6_000)],
    ids=["eom-at-start", "eom-before-start", "eom-after-stop"],
)
def test_save_eom_equal_start_refused(rig: Rig, start_ms: int, eom_ms: int, stop_ms: int) -> None:
    """LR11: the liner rule, span > 0 and EOM within the span (start < eom <= stop): markers
    out of order raise the domain's InvalidStreamValueError; nothing is stored or committed."""
    with pytest.raises(InvalidStreamValueError):
        save_liner_cues(rig.ports, STATION_ID, LINER_ID, start_ms, eom_ms, stop_ms)
    assert rig.stored() is None
    assert rig.commits == []


def test_save_a_400_ms_liner_is_allowed(rig: Rig) -> None:
    """LR4, LR11: no D9 minimum (fade-out + 3 s): a 400 ms liner saves (0, 300, 400)."""
    rig.liners.liners[LINER_ID] = station_liner(duration_ms=400)
    save_liner_cues(rig.ports, STATION_ID, LINER_ID, 0, 300, 400)
    assert rig.stored() == CueMarkers(start_ms=0, eom_ms=300, fade_at_ms=400, stop_ms=400)
    assert rig.commits == [CueMarkers(0, 300, 400, 400)]


def test_save_a_one_ms_span_is_allowed(rig: Rig) -> None:
    """LR4, LR11: the liner rule needs only span > 0 with EOM inside it."""
    save_liner_cues(rig.ports, STATION_ID, LINER_ID, 6_999, 7_000, 7_000)
    assert rig.stored() == CueMarkers(6_999, 7_000, 7_000, 7_000)


def test_save_silent_liner_refused(rig: Rig) -> None:
    """LR4, LR11, D111: the radio never plays a silent liner, so an edit is refused with this
    sentence; nothing is stored or committed."""
    rig.measure_silent()
    with pytest.raises(CueEditRejectedError) as raised:
        save_liner_cues(rig.ports, STATION_ID, LINER_ID, 500, 6_000, 6_800)
    assert str(raised.value) == SILENT_REFUSAL
    assert rig.stored() is None
    assert rig.commits == []


# --- Reset (LR11, LR4) ---------------------------------------------------------------------


def test_reset_clears_and_commits(rig: Rig) -> None:
    """LR4, LR11: reset clears the liner's edit, then commits; the view shows no manual."""
    rig.liners.save_markers(LINER_ID, EDIT)
    assert reset_liner_cues(rig.ports, STATION_ID, LINER_ID) is None
    assert rig.stored() is None
    assert rig.commits == [None]
    assert read_liner_cues(rig.ports, STATION_ID, LINER_ID).manual is None


def test_reset_leaves_other_liners_edits(rig: Rig) -> None:
    """LR4: reset clears this liner's edit only, even for another liner with the same audio."""
    rig.add(station_liner(TWIN_ID, station_id=OTHER_STATION_ID, markers=EDIT))
    rig.liners.save_markers(LINER_ID, EDIT)
    reset_liner_cues(rig.ports, STATION_ID, LINER_ID)
    assert rig.stored(TWIN_ID) == EDIT


def test_reset_still_works_on_a_silent_liner(rig: Rig) -> None:
    """LR4, D111: an edit left from before the liner was measured silent can be removed."""
    rig.liners.save_markers(LINER_ID, EDIT)
    rig.measure_silent()
    reset_liner_cues(rig.ports, STATION_ID, LINER_ID)
    assert rig.stored() is None
    assert rig.commits == [None]


# --- Not found (LR3, LR11) -----------------------------------------------------------------

CALLS: list[tuple[str, Callable[[Rig, UUID, UUID], object]]] = [
    ("view", lambda r, s, i: read_liner_cues(r.ports, s, i)),
    ("save", lambda r, s, i: save_liner_cues(r.ports, s, i, 500, 6_000, 6_800)),
    ("reset", lambda r, s, i: reset_liner_cues(r.ports, s, i)),
    ("waveform", lambda r, s, i: read_liner_waveform(r.ports, s, i, never_read)),
]
CALL_IDS = [name for name, _ in CALLS]


@pytest.mark.parametrize("call", [call for _, call in CALLS], ids=CALL_IDS)
def test_other_stations_liner_not_found(
    rig: Rig, call: Callable[[Rig, UUID, UUID], object]
) -> None:
    """LR3, LR11: a liner asked for under another station is not found, on every call, even
    with its file on disk under both stations' folders (so a build that looks only under the
    asked-for station's folder can't pass by finding no file); the peak reader never runs, the
    edit is untouched and nothing is committed (audit M3)."""
    rig.liners.save_markers(LINER_ID, EDIT)
    rig.put_on_disk(station_liner())
    rig.put_on_disk(station_liner(station_id=OTHER_STATION_ID))
    with pytest.raises(LinerNotFoundError):
        call(rig, OTHER_STATION_ID, LINER_ID)
    assert rig.stored() == EDIT
    assert rig.commits == []


@pytest.mark.parametrize("call", [call for _, call in CALLS], ids=CALL_IDS)
def test_unknown_liner_not_found(rig: Rig, call: Callable[[Rig, UUID, UUID], object]) -> None:
    """LR11: a liner id that does not exist is not found, on every call."""
    with pytest.raises(LinerNotFoundError):
        call(rig, STATION_ID, uuid4())
    assert rig.commits == []


# --- The waveform (LR11) -------------------------------------------------------------------


def test_waveform_reads_the_stored_file(rig: Rig) -> None:
    """LR11: the reader is given the liner's stored file, ``<liners root>/<station>/<file>``;
    the waveform is its peaks with the liner's own length."""
    path = rig.put_on_disk(station_liner())
    asked: list[str] = []

    def read(target: str) -> Peaks:
        asked.append(target)
        return Peaks(200, b"\x01\x02")

    waveform = read_liner_waveform(rig.ports, STATION_ID, LINER_ID, read)
    assert waveform == Waveform(duration_ms=DURATION_MS, per_second=200, peaks=b"\x01\x02")
    assert [Path(target) for target in asked] == [path]
    assert rig.commits == []


def test_waveform_missing_file_not_found(rig: Rig) -> None:
    """LR11: the row exists but its file is gone from disk: not found, and the reader is never
    asked to decode a file that is not there."""

    def read(target: str) -> Peaks:
        raise AssertionError(f"the reader must not run for a file that is gone ({target})")

    with pytest.raises(LinerNotFoundError):
        read_liner_waveform(rig.ports, STATION_ID, LINER_ID, read)


def test_waveform_reader_failure_is_unreadable(rig: Rig) -> None:
    """LR11: a PeakReadError from the reader becomes the domain's CueAudioUnreadableError."""
    rig.put_on_disk(station_liner())

    def read(target: str) -> Peaks:
        raise PeakReadError("Invalid data found when processing input")

    with pytest.raises(CueAudioUnreadableError):
        read_liner_waveform(rig.ports, STATION_ID, LINER_ID, read)
