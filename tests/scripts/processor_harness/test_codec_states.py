"""The MP3 encoder's states: a whole song encoded after a lead-in of silence, decoded, its
per-block true peaks back on the song's own timeline; and the screening pick of the worst blocks."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, codec, codec_states, signals
from processor_harness.codec_states import Block

FS = 44100
FFMPEG = shutil.which("ffmpeg")


def test_worst_blocks_come_worst_first_and_two_seconds_apart_within_a_song() -> None:
    a = np.full(100, -3.0)
    a[[10, 12, 40]] = [-0.5, -0.6, -0.7]  # 12 is 0.2 s from 10: the same transient
    b = np.full(100, -3.0)
    b[11] = -0.55  # another song: no spacing against song a

    picked = codec_states.worst_blocks({"a": a, "b": b}, count=3, separation_s=2.0)

    assert picked == [Block("a", 10, -0.5), Block("b", 11, -0.55), Block("a", 40, -0.7)]
    assert picked[0].time_s == pytest.approx(1.0)


def test_worst_blocks_stops_at_the_count() -> None:
    a = np.linspace(-3.0, -1.0, 100)

    assert len(codec_states.worst_blocks({"a": a}, count=2, separation_s=2.0)) == 2


@pytest.mark.slow
@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
def test_no_lead_in_is_the_harness_codec_round_trip(tmp_path: Path) -> None:
    # Samples piped to FFmpeg as float32 must encode exactly as the float WAV the codec
    # module writes, so lead-in 0 reproduces a render's own MP3.
    assert FFMPEG is not None
    x = signals.noise_with_pauses(FS, burst_s=2.0, pause_s=0.5, bursts=1, level_dbfs=-12.0).samples
    wav = tmp_path / "x.wav"
    audio_io.write_wav(wav, x, FS)
    mp3 = codec.encode_mp3(Path(FFMPEG), wav, tmp_path / "x.mp3")
    expected, _ = audio_io.read_wav(codec.decode(Path(FFMPEG), mp3, tmp_path / "d.wav"))

    got = codec_states.encode_decode(Path(FFMPEG), x, FS, lead_in=0)

    np.testing.assert_array_equal(got, expected)


@pytest.mark.slow
@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
@pytest.mark.parametrize("lead_in", [0, 700])
def test_the_lead_in_comes_off_again(lead_in: int) -> None:
    assert FFMPEG is not None
    x = np.zeros((3 * FS, 2))
    # A click late in the block that starts at 1.0 s (4410 samples long): a lead-in left on, or
    # the encoder's own delay, would push it into the next block.
    x[FS + 4000] = 0.5

    peaks = codec_states.block_peaks(Path(FFMPEG), x, FS, lead_in)

    assert int(np.argmax(peaks)) == 10
    assert len(peaks) == 30
