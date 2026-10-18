"""Waveform peaks read with FFmpeg (cue editor PR 2, Task 5; requirement R7, plan note 10).

``read_peaks(path, settings)`` decodes a file to mono s16le at ``sample_rate`` and keeps the
largest absolute sample of every ``sample_rate // per_second`` samples, scaled
``round(255 * max / 32768)``: one 0-255 byte per bucket, a partial last bucket counting. The
editor draws its waveform from these bytes (200 per second by default).

Fixtures are real audio made by FFmpeg itself into ``tmp_path`` (lavfi ``sine`` and
``anullsrc``), written at 8 kHz so the decoded length is exact and the bucket counts below are
exact. A module of real FFmpeg runs: it is ``slow`` and skipped when FFmpeg is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend.config import get_settings
from backend.playout.peaks import PeakReadError, Peaks, PeakSettings, read_peaks

# FFmpeg as the app finds it: the configured path first, then PATH.
FFMPEG = shutil.which(get_settings().ffmpeg_path) or shutil.which("ffmpeg")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not found (settings.ffmpeg_path, PATH)"),
]

# The sine source plays at 1/8 of full scale; +12 dB makes it 0.498, i.e. about -6 dBFS.
LOUD_SINE = "sine=frequency={frequency}:sample_rate=8000:duration={seconds},volume=12dB"


def ffmpeg(*args: str) -> None:
    assert FFMPEG is not None
    subprocess.run([FFMPEG, "-v", "error", "-y", *args], check=True, capture_output=True)


def tone(path: Path, seconds: float, *, frequency: int = 440) -> Path:
    """A mono 8 kHz sine at about -6 dBFS, ``seconds`` long."""
    graph = LOUD_SINE.format(frequency=frequency, seconds=seconds)
    ffmpeg("-f", "lavfi", "-i", graph, "-c:a", "pcm_s16le", str(path))
    return path


def settings(*, per_second: int = 200, timeout_s: float = 60.0) -> PeakSettings:
    assert FFMPEG is not None
    return PeakSettings(ffmpeg=FFMPEG, per_second=per_second, timeout_s=timeout_s)


def test_the_defaults_are_200_a_second_from_8_khz_with_a_minute_to_decode() -> None:
    """Interfaces: PeakSettings defaults (plan note 10: 200 peaks a second, 40-sample buckets)."""
    defaults = PeakSettings(ffmpeg="ffmpeg")
    assert (defaults.per_second, defaults.sample_rate, defaults.timeout_s) == (200, 8000, 60.0)


def test_two_seconds_give_400_peaks(tmp_path: Path) -> None:
    """R7, Task 5: 2.0 s at 200 a second is 400 bytes, and the result says it is 200 a second."""
    peaks = read_peaks(str(tone(tmp_path / "two.wav", 2.0)), settings())
    assert isinstance(peaks, Peaks)
    assert peaks.per_second == 200
    assert isinstance(peaks.data, bytes)
    assert len(peaks.data) == 400


def test_silence_then_tone_is_visible(tmp_path: Path) -> None:
    """R7, Task 5: a second of silence then a second of tone shows as quiet then loud peaks."""
    path = tmp_path / "silence-then-tone.wav"
    ffmpeg(
        "-filter_complex",
        "anullsrc=r=8000:cl=mono:d=1,atrim=end_sample=8000[quiet];"
        + LOUD_SINE.format(frequency=440, seconds=1)
        + "[tone];[quiet][tone]concat=n=2:v=0:a=1[out]",
        "-map",
        "[out]",
        "-c:a",
        "pcm_s16le",
        str(path),
    )
    data = read_peaks(str(path), settings()).data
    assert len(data) == 400
    assert max(data[:190]) <= 2
    assert min(data[210:390]) >= 100


def test_a_partial_last_bucket_counts(tmp_path: Path) -> None:
    """R7, Task 5: 1.0025 s is 200.5 buckets of 40 samples, so the half bucket makes 201."""
    peaks = read_peaks(str(tone(tmp_path / "partial.wav", 1.0025)), settings())
    assert len(peaks.data) == 201
    assert peaks.data[-1] > 0, "the partial bucket holds real samples, not padding"


def test_a_peak_is_a_fraction_of_full_scale_in_255ths(tmp_path: Path) -> None:
    """R7, Task 5: round(255 * max / 32768): a tone at 16302 of 32768 reads 127 (about -6 dBFS)."""
    data = read_peaks(str(tone(tmp_path / "scale.wav", 1.0, frequency=1000)), settings()).data
    assert set(data[10:190]) <= {126, 127, 128}
    assert 127 in data


def test_full_scale_reads_255_whatever_the_sign(tmp_path: Path) -> None:
    """R7, Task 5: a sample at -32768 (clipped audio, which int16 abs() cannot negate) is full
    scale, 255, and so is +1.0 (32767): the largest magnitude must not overflow or wrap."""
    low = tmp_path / "low.wav"
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=-1:s=8000:d=1", "-c:a", "pcm_s16le", str(low))
    high = tmp_path / "high.wav"
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=1:s=8000:d=1", "-c:a", "pcm_s16le", str(high))
    assert read_peaks(str(low), settings()).data == bytes([255]) * 200
    assert read_peaks(str(high), settings()).data == bytes([255]) * 200


def test_per_second_sets_the_bucket_size(tmp_path: Path) -> None:
    """Interfaces: 100 a second means 80-sample buckets: 2.0 s gives 200 peaks at 100 a second."""
    peaks = read_peaks(str(tone(tmp_path / "coarse.wav", 2.0)), settings(per_second=100))
    assert (peaks.per_second, len(peaks.data)) == (100, 200)


def test_a_stereo_flac_at_cd_rate_is_mixed_down_to_one_set_of_peaks(tmp_path: Path) -> None:
    """R7, Task 5: the library is mostly 44.1 kHz stereo FLAC; peaks are one per bucket, not per
    channel (``-ac 1``). A resampler may add or drop a sample, so one bucket of slack."""
    path = tmp_path / "stereo.flac"
    ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2", "-ac", "2",
        "-c:a", "flac", str(path),
    )  # fmt: skip
    peaks = read_peaks(str(path), settings())
    assert 399 <= len(peaks.data) <= 401
    assert max(peaks.data) > 0


def test_a_path_with_spaces_and_accents_is_read(tmp_path: Path) -> None:
    """R7, Task 5: library paths look like 'Slipknot - Été (live).wav'; the path is one argument."""
    path = tone(tmp_path / "Slipknot - Été (live).wav", 1.0)
    assert len(read_peaks(str(path), settings()).data) == 200


def test_unreadable_file_raises(tmp_path: Path) -> None:
    """R7, Task 5: a file FFmpeg cannot decode (a text file) is a PeakReadError with a reason."""
    path = tmp_path / "notes.flac"
    path.write_text("this is not audio", encoding="utf-8")
    with pytest.raises(PeakReadError) as raised:
        read_peaks(str(path), settings())
    assert str(raised.value).strip(), "the error carries FFmpeg's last stderr line"


def test_a_file_that_is_not_there_raises(tmp_path: Path) -> None:
    """R7, Task 5: a path that does not exist is a PeakReadError, never a bare FFmpeg failure."""
    with pytest.raises(PeakReadError):
        read_peaks(str(tmp_path / "gone.flac"), settings())


def test_missing_ffmpeg_raises(tmp_path: Path) -> None:
    """R7, Task 5: no FFmpeg program at all (FileNotFoundError underneath) is a PeakReadError."""
    path = tone(tmp_path / "fine.wav", 1.0)
    with pytest.raises(PeakReadError) as raised:
        read_peaks(str(path), PeakSettings(ffmpeg="no-such-ffmpeg"))
    assert str(raised.value).strip()


def test_a_decode_that_outlives_the_timeout_raises(tmp_path: Path) -> None:
    """R7, Task 5: subprocess.TimeoutExpired becomes PeakReadError. FFmpeg cannot even start
    in a millisecond, so the timeout always fires first."""
    path = tone(tmp_path / "slow-start.wav", 1.0)
    with pytest.raises(PeakReadError):
        read_peaks(str(path), settings(timeout_s=0.001))
