"""Codec round trip (P5): the stream's MP3 settings, decoded back to float for true peak."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, codec, signals
from processor_harness.metrics import loudness

FS = 44100
FFMPEG = shutil.which("ffmpeg")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH"),
]


def aligned_tone_level(reference: np.ndarray, decoded: np.ndarray, freq: float) -> float:
    """The decoded tone's amplitude (dB) in its steady middle, after lining it up with the
    reference by cross-correlation (LAME adds encoder delay and padding)."""
    n = len(reference) + len(decoded)
    corr = np.fft.irfft(np.fft.rfft(decoded, n) * np.conj(np.fft.rfft(reference, n)), n)
    lag = int(np.argmax(corr[:FS]))
    steady = decoded[lag + FS : lag + 2 * FS]
    t = np.arange(len(steady)) / FS
    basis = np.column_stack([np.sin(2 * np.pi * freq * t), np.cos(2 * np.pi * freq * t)])
    coef, *_ = np.linalg.lstsq(basis, steady, rcond=None)
    return float(20 * np.log10(np.hypot(*coef)))


def round_trip_level(tmp_path: Path, bitrate: str) -> float:
    assert FFMPEG is not None
    tone = signals.tone(FS, 1000.0, -12.0, 3.0).samples
    wav = tmp_path / "in.wav"
    audio_io.write_wav(wav, tone, FS)

    mp3 = codec.encode_mp3(Path(FFMPEG), wav, tmp_path / "in.mp3", bitrate=bitrate)
    decoded, fs = audio_io.read_wav(codec.decode(Path(FFMPEG), mp3, tmp_path / "decoded.wav"))

    assert fs == FS and decoded.shape[1] == 2
    return aligned_tone_level(tone[:, 0], decoded[:, 0], 1000.0)


def test_round_trip_at_the_streams_128k_carries_lames_preset_scale(tmp_path: Path) -> None:
    expected = -12.0 + 20 * np.log10(codec.LAME_SCALE_AT_128K)  # -12.45 dB

    assert round_trip_level(tmp_path, "128k") == pytest.approx(expected, abs=0.05)


def test_round_trip_at_256k_keeps_the_level(tmp_path: Path) -> None:
    # LAME's preset scale is 1.0 from 256 kb/s, so the tone comes back where it went in.
    assert round_trip_level(tmp_path, "256k") == pytest.approx(-12.0, abs=0.3)


def test_round_trip_overshoots_a_hard_limited_signal(tmp_path: Path) -> None:
    # Pink noise clipped flat at -1 dBFS: the encoder's band-limiting rebuilds peaks above
    # the clip level. This overshoot is what P5's ceiling margin has to absorb.
    assert FFMPEG is not None
    noise = signals.noise_with_pauses(FS, burst_s=5.0, pause_s=0.1, bursts=1, level_dbfs=-6.0)
    ceiling = 10 ** (-1.0 / 20)
    wav = tmp_path / "clipped.wav"
    audio_io.write_wav(wav, np.clip(noise.samples, -ceiling, ceiling), FS)

    decoded = codec.decode(
        Path(FFMPEG), codec.encode_mp3(Path(FFMPEG), wav, tmp_path / "c.mp3"), tmp_path / "d.wav"
    )

    x, _ = audio_io.read_wav(decoded)
    assert loudness.true_peak(x, FS) > -1.0


def test_ffmpeg_errors_keep_non_ascii_file_names(tmp_path: Path) -> None:
    # FFmpeg's messages are UTF-8: read with Windows' codepage, a name like blink‐182's (U+2010)
    # crashed the reader thread and the error text was lost.
    assert FFMPEG is not None
    missing = tmp_path / "blink‐182 - Stay Together.mp3"

    with pytest.raises(RuntimeError, match="blink‐182"):
        codec.decode(Path(FFMPEG), missing, tmp_path / "out.wav")
