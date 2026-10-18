"""The cue editor's dependency providers (cue editor PR 2, Task 7; R7, plan note 12).

``get_cue_editor_ports`` builds the service's ports from the request's repositories,
``get_peak_reader`` returns FFmpeg's peak reader bound to ``settings.ffmpeg_path``, and
``get_stream_timing`` returns the radio's timing. The route tests override all three; this file
checks the real ones, with no database.
"""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from typing import cast, get_type_hints

import pytest

import backend.dependencies as dependencies
import backend.playout.peaks as peaks_module
from backend.config import get_settings
from backend.dependencies import (
    SyncRepos,
    get_cue_editor_ports,
    get_peak_reader,
    get_stream_timing,
)
from backend.domain.streaming import StreamTiming
from backend.playout.peaks import Peaks, PeakSettings
from backend.services.repository_factory import RepositoryFactory
from backend.services.streaming.cue_editor import CueEditorPorts
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.stream_cue_edits import FakeStreamCueEditRepository
from tests.fakes.stream_cues import FakeStreamCueRepository

CONFIGURED_FFMPEG = r"C:\Tools\ffmpeg\bin\ffmpeg.exe"


@pytest.fixture
def configured_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    """``FFMPEG_PATH`` set for the settings the providers read, restored afterwards."""
    monkeypatch.setenv("FFMPEG_PATH", CONFIGURED_FFMPEG)
    get_settings.cache_clear()
    try:
        yield CONFIGURED_FFMPEG
    finally:
        get_settings.cache_clear()


def test_the_peak_reader_runs_ffmpeg_from_the_configured_path(
    monkeypatch: pytest.MonkeyPatch, configured_ffmpeg: str
) -> None:
    """R7, Task 7: ``get_peak_reader()(path)`` calls ``read_peaks(path, PeakSettings(ffmpeg=
    settings.ffmpeg_path))`` and returns its peaks.

    ``read_peaks`` is replaced where it is defined and where the dependencies module may have
    imported it by name, so the test holds for either import style.
    """
    seen: list[tuple[str, PeakSettings]] = []
    answer = Peaks(per_second=200, data=b"\x07\x08")

    def fake_read_peaks(path: str, settings: PeakSettings) -> Peaks:
        seen.append((path, settings))
        return answer

    monkeypatch.setattr(peaks_module, "read_peaks", fake_read_peaks)
    monkeypatch.setattr(dependencies, "read_peaks", fake_read_peaks, raising=False)

    result = get_peak_reader()("D:/Music/Slipknot/heretic.flac")

    assert result == answer
    assert seen == [("D:/Music/Slipknot/heretic.flac", PeakSettings(ffmpeg=configured_ffmpeg))]


def test_the_peak_reader_follows_a_change_of_ffmpeg_path(
    monkeypatch: pytest.MonkeyPatch, configured_ffmpeg: str
) -> None:
    """R7, Task 7: the path comes from the settings, not from a constant: a reader built after
    the setting changed uses the new one."""
    used: list[str] = []

    def fake_read_peaks(path: str, settings: PeakSettings) -> Peaks:
        used.append(settings.ffmpeg)
        return Peaks(per_second=200, data=b"")

    monkeypatch.setattr(peaks_module, "read_peaks", fake_read_peaks)
    monkeypatch.setattr(dependencies, "read_peaks", fake_read_peaks, raising=False)
    monkeypatch.setenv("FFMPEG_PATH", "/opt/other/ffmpeg")
    get_settings.cache_clear()

    get_peak_reader()("a.flac")

    assert used == ["/opt/other/ffmpeg"]


def test_the_ports_are_built_from_the_request_repositories() -> None:
    """R7, Task 7: files from ``repos.library_files``, cues from ``repos.streaming.cues``,
    edits from ``repos.streaming.edits``: the very repositories, not copies."""
    files = FakeLibraryFileRepository()
    cues = FakeStreamCueRepository()
    edits = FakeStreamCueEditRepository()
    repos = SimpleNamespace(library_files=files, streaming=SimpleNamespace(cues=cues, edits=edits))

    ports = get_cue_editor_ports(cast(RepositoryFactory, repos))

    assert isinstance(ports, CueEditorPorts)
    assert ports.files is files
    assert ports.cues is cues
    assert ports.edits is edits


def test_the_ports_take_the_request_scoped_repositories() -> None:
    """R7, plan note 12: the provider is declared ``get_cue_editor_ports(repos: SyncRepos)``,
    one connection per request, committed before the response."""
    hints = get_type_hints(get_cue_editor_ports, include_extras=True)
    assert hints["repos"] == SyncRepos


def test_the_stream_timing_is_the_radios_default() -> None:
    """R6, Task 7: Save checks the edit against ``StreamTiming()``, the timing the radio uses."""
    assert get_stream_timing() == StreamTiming()
