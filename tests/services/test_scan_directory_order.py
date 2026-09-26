"""Characterization tests for the order and content of scan_directory's output.

The scan task writes rows and groups files in the order scan_directory hands
them over, and grouping is order-dependent (the first file of a song creates
its work and becomes its preferred file). Any change to how extraction is
scheduled must leave callers seeing exactly this sequence.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.domain.library import LibraryFile, LibraryQuarantine
from backend.services.library_scan_service import scan_directory

AUDIO_DIR = Path(__file__).parent.parent / "fixtures" / "audio"
_SOURCES = ["well_tagged.mp3", "partial_tags.mp3", "minimal_tags.ogg", "no_tags.wav", "corrupt.mp3"]

Event = tuple[object, ...]


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A nested library of 130 files, every tenth one corrupt, plus non-audio noise."""
    for src in _SOURCES:
        if not (AUDIO_DIR / src).exists():
            pytest.skip(f"Fixture not found: {src}")
    root = tmp_path / "lib"
    for i in range(130):
        src = _SOURCES[4] if i % 10 == 3 else _SOURCES[i % 4]
        # One capitalised artist: str order puts "Artist1" before "artist0",
        # Path order on Windows does not, so the test pins the real ordering.
        artist = f"Artist{i % 7}" if i % 7 == 1 else f"artist{i % 7}"
        folder = root / artist / f"album{i % 3}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(AUDIO_DIR / src, folder / f"{i:03d}{Path(src).suffix}")
    (root / "artist0" / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (root / "notes.txt").write_text("not audio")
    return root


def _file_event(lf: LibraryFile) -> Event:
    a = lf.audio
    return (
        "file", lf.file_path, lf.file_hash, lf.format, lf.file_size, lf.file_mtime_ns,
        a.track_title, a.artist_name, a.recording_mbid, a.duration_ms, a.bitrate,
        tuple(sorted((a.raw_metadata or {}).items())),
    )


def _record(root: Path) -> tuple[list[Event], list[LibraryFile], list[LibraryQuarantine]]:
    events: list[Event] = []
    files, quarantine = scan_directory(
        root,
        on_file=lambda lf: events.append(_file_event(lf)),
        on_quarantine=lambda q: events.append(("quarantine", q.file_path, q.error_message)),
        on_progress=lambda done, total, path: events.append(("progress", done, total, path)),
    )
    return events, files, quarantine


class TestScanDirectoryOrder:
    def test_callbacks_follow_path_sort_order(self, library: Path) -> None:
        # scan_directory sorts Path objects, whose ordering is per component
        # and (on Windows) case-folded, so pin that rather than str order.
        events, _, _ = _record(library)
        paths = [Path(str(e[1])) for e in events if e[0] in ("file", "quarantine")]
        assert paths == sorted(paths)
        assert len(paths) == 130

    def test_progress_every_50_files_and_on_the_last(self, library: Path) -> None:
        events, _, _ = _record(library)
        progress = [(e[1], e[2]) for e in events if e[0] == "progress"]
        assert progress == [(50, 130), (100, 130), (130, 130)]

    def test_progress_reported_after_the_file_it_counts(self, library: Path) -> None:
        events, _, _ = _record(library)
        for i, event in enumerate(events):
            if event[0] == "progress":
                assert events[i - 1][1] == event[3]

    def test_return_lists_match_callbacks(self, library: Path) -> None:
        events, files, quarantine = _record(library)
        assert [_file_event(lf) for lf in files] == [e for e in events if e[0] == "file"]
        assert [("quarantine", q.file_path, q.error_message) for q in quarantine] == [
            e for e in events if e[0] == "quarantine"
        ]
        assert len(quarantine) == 13
