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


def test_round_trip_keeps_a_quiet_tone_at_its_level(tmp_path: Path) -> None:
    assert FFMPEG is not None
    wav = tmp_path / "in.wav"
    audio_io.write_wav(wav, signals.tone(FS, 1000.0, -12.0, 3.0).samples, FS)

    mp3 = codec.encode_mp3(Path(FFMPEG), wav, tmp_path / "in.mp3")
    decoded = codec.decode(Path(FFMPEG), mp3, tmp_path / "decoded.wav")

    x, fs = audio_io.read_wav(decoded)
    assert fs == FS and x.shape[1] == 2
    # Measured 2026-10-24 (FFmpeg 8.0.1's libmp3lame, 128k): a 1 kHz tone comes back 0.45 dB
    # low at -20, -12 and -6 dBFS alike.
    assert 20 * np.log10(np.max(np.abs(x))) == pytest.approx(-12.0, abs=0.6)


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
