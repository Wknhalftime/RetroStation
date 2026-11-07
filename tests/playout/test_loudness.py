"""Loudness tail read with FFmpeg (EOM ending rules, stage 1, Task 2).

``read_tail(path, cue_out_ms, settings)`` decodes a file to mono at 16000 Hz (so a 50 ms frame
is exactly 800 samples), takes 50 ms frames of mean-square energy, and gives each frame in dB
relative to the mean energy of the WHOLE song's frames louder than -60 dBFS. Frame ``i`` of the
tail covers ``start_ms + 50*i`` to ``start_ms + 50*(i+1)``. The tail runs from
``cue_out_ms - tail_ms`` (or the start) to the end of the file. The fixture script
``scripts/extract_ending_fixtures.py`` follows the same definition, so these tests pin it.

Fixtures are WAV files written with numpy into ``tmp_path``, at 16000 Hz. A module of real
FFmpeg runs: it is ``slow`` and skipped when FFmpeg is not installed.
"""

from __future__ import annotations

import math
import shutil
import stat
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from backend.config import get_settings
from backend.domain.endings import LoudnessTail
from backend.playout.loudness import (
    LoudnessReadError,
    LoudnessSettings,
    LoudnessToolError,
    read_tail,
)

FFMPEG = shutil.which(get_settings().ffmpeg_path) or shutil.which("ffmpeg")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not found (settings.ffmpeg_path, PATH)"),
]

RATE = 16000
SILENT: float | None = None
"""A part with no signal at all (digital silence)."""


def write_wav(path: Path, parts: list[tuple[float, float | None]]) -> Path:
    """A mono 16-bit WAV at 16000 Hz: each part is (seconds, sine peak in dBFS, or None for
    digital silence)."""
    chunks = []
    for seconds, dbfs in parts:
        count = round(seconds * RATE)
        if dbfs is None:
            chunks.append(np.zeros(count))
        else:
            t = np.arange(count) / RATE
            chunks.append(10 ** (dbfs / 20) * np.sin(2 * math.pi * 440 * t))
    samples = np.concatenate(chunks)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((samples * 32767).astype("<i2").tobytes())
    return path


def sine_energy(dbfs: float) -> float:
    """A sine's mean square: half its peak squared."""
    return float(10 ** (dbfs / 10) / 2)


def settings() -> LoudnessSettings:
    assert FFMPEG is not None
    return LoudnessSettings(ffmpeg=FFMPEG)


def level_at(tail: LoudnessTail, ms: int) -> float:
    return tail.level_db[(ms - tail.start_ms) // tail.frame_ms]


def test_defaults() -> None:
    defaults = LoudnessSettings(ffmpeg="ffmpeg")
    assert (defaults.sample_rate, defaults.frame_ms, defaults.tail_ms) == (16000, 50, 60_000)
    assert (defaults.floor_db, defaults.timeout_s) == (-60.0, 60.0)


def test_reference_is_the_whole_song(tmp_path: Path) -> None:
    """70 s loud then 20 s 20 dB quieter, Stop at 90 s: the tail starts at 30 s, but the
    reference is the mean over all 90 s (1400 loud frames, 400 quiet), not over the tail."""
    path = write_wav(tmp_path / "steps.wav", [(70.0, -6.0), (20.0, -26.0)])
    tail = read_tail(str(path), 90_000, settings())
    assert isinstance(tail, LoudnessTail)
    assert (tail.start_ms, tail.frame_ms) == (30_000, 50)
    loud, quiet = sine_energy(-6.0), sine_energy(-26.0)
    reference = 10 * math.log10((1400 * loud + 400 * quiet) / 1800)
    assert level_at(tail, 40_000) == pytest.approx(10 * math.log10(loud) - reference, abs=0.3)
    assert level_at(tail, 85_000) == pytest.approx(10 * math.log10(quiet) - reference, abs=0.3)


def test_silence_is_left_out_of_the_reference(tmp_path: Path) -> None:
    """10 s loud then 10 s of digital silence: the silent frames (below -60 dBFS) do not
    count, so the loud part sits at 0 dB, not 3 dB above a mean diluted by silence."""
    path = write_wav(tmp_path / "loud-then-silent.wav", [(10.0, -6.0), (10.0, SILENT)])
    tail = read_tail(str(path), 20_000, settings())
    assert level_at(tail, 5_000) == pytest.approx(0.0, abs=0.3)
    assert level_at(tail, 15_000) < -60.0


def test_frame_index_maps_to_time(tmp_path: Path) -> None:
    """A step from loud to quiet at exactly 80 s of a 90 s file, Stop at 90 s: frame 999 of
    the tail (79.95-80.00 s) is loud and frame 1000 (80.00-80.05 s) is quiet."""
    path = write_wav(tmp_path / "step-at-80.wav", [(80.0, -6.0), (10.0, -40.0)])
    tail = read_tail(str(path), 90_000, settings())
    assert tail.start_ms == 30_000
    assert tail.level_db[999] - tail.level_db[1000] == pytest.approx(34.0, abs=0.5)
    assert len(tail.level_db) == 1200


def test_tail_starts_tail_ms_before_cue_out(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "long.wav", [(90.0, -10.0)])
    tail = read_tail(str(path), 80_000, settings())
    assert tail.start_ms == 20_000
    assert len(tail.level_db) * 50 == pytest.approx(70_000, abs=100)


def test_short_file_tail_starts_at_zero(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "short.wav", [(10.0, -10.0)])
    tail = read_tail(str(path), 10_000, settings())
    assert tail.start_ms == 0
    assert len(tail.level_db) * 50 == pytest.approx(10_000, abs=100)


def test_unreadable_file_raises_read_error(tmp_path: Path) -> None:
    path = tmp_path / "not-audio.flac"
    path.write_text("this is not audio", encoding="utf-8")
    with pytest.raises(LoudnessReadError):
        read_tail(str(path), 10_000, settings())


def test_missing_file_raises_read_error(tmp_path: Path) -> None:
    with pytest.raises(LoudnessReadError):
        read_tail(str(tmp_path / "gone.flac"), 10_000, settings())


def test_missing_ffmpeg_raises_tool_error(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "ok.wav", [(2.0, -10.0)])
    with pytest.raises(LoudnessToolError):
        read_tail(str(path), 2_000, LoudnessSettings(ffmpeg="no-such-ffmpeg-exe"))


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX executable script")
def test_ffmpeg_that_is_not_ffmpeg_raises_read_error(tmp_path: Path) -> None:
    """An executable that runs but is not FFmpeg fails per file (the run's breaker, Task 6,
    is what stops a whole library being marked unmeasured)."""
    fake = tmp_path / "fake-ffmpeg"
    fake.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    path = write_wav(tmp_path / "ok.wav", [(2.0, -10.0)])
    with pytest.raises(LoudnessReadError):
        read_tail(str(path), 2_000, LoudnessSettings(ffmpeg=str(fake)))
