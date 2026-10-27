"""The MP3 encoder's states (R17; Lance, 2026-10-26). At the stream's 128k the decoded true peak
of a transient depends on the encoder's state when the song reaches it: where the song falls in
the encoder's frame grid (a frame is 1152 samples) and everything it encoded before. On air that
state is random, and it moves one transient's decoded peak by up to about 2 dB.

A state here is a lead-in of silence ahead of the whole song. The memory is long, so an excerpt
with a few seconds of pre-roll does not reproduce a whole-song encode (measured 2026-10-26: at
Hotel California 161.8 s, 576 excerpt encodes never came within 0.4 dB of the worst of 576
whole-song ones), and every encode here is of the song from its start. Lead-ins of 0 to 1151
samples cover one whole frame. With no lead-in the encode matches the render's own MP3 exactly.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from processor_harness.audio_io import Audio
from processor_harness.codec import MP3_BITRATE, MP3_RATE
from processor_harness.metrics.loudness import STEP_S, true_peak_blocks

LEAD_INS = 1152  # one MP3 frame: two 576-sample granules


def _ffmpeg(ffmpeg: Path, *args: str, data: bytes = b"") -> bytes:
    run = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostats", "-y", *args], input=data, capture_output=True
    )
    if run.returncode != 0:
        tail = run.stderr.decode("utf-8", errors="replace")[-4000:]
        raise RuntimeError(f"ffmpeg {' '.join(args)} failed:\n{tail}")
    return run.stdout


def encode_decode(ffmpeg: Path, x: Audio, fs: int, lead_in: int) -> Audio:
    """x through the stream's codec after ``lead_in`` samples of silence, on x's own timeline.
    Samples go to FFmpeg as float32, the values the harness's float WAVs carry."""
    channels = x.shape[1]
    padded = np.concatenate([np.zeros((lead_in, channels)), x]).astype(np.float32)
    with tempfile.TemporaryDirectory() as tmp:
        mp3 = Path(tmp) / "x.mp3"
        _ffmpeg(ffmpeg, "-f", "f32le", "-ar", str(fs), "-ac", str(channels), "-i", "pipe:0",
                "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-ar", str(MP3_RATE), str(mp3),
                data=padded.tobytes())  # fmt: skip
        raw = _ffmpeg(ffmpeg, "-i", str(mp3), "-f", "f32le", "-c:a", "pcm_f32le", "pipe:1")
    decoded = np.frombuffer(raw, np.float32).reshape(-1, channels).astype(np.float64)
    return decoded[lead_in : lead_in + len(x)]


def block_peaks(ffmpeg: Path, x: Audio, fs: int, lead_in: int) -> NDArray[np.float64]:
    """Decoded true peak (dBTP) of each 100 ms block of x, after a lead-in of ``lead_in``."""
    peaks = true_peak_blocks(encode_decode(ffmpeg, x, fs, lead_in), fs)
    return np.asarray(20 * np.log10(np.maximum(peaks, 1e-12)), dtype=np.float64)


@dataclass(frozen=True)
class Block:
    song: str
    index: int  # 100 ms blocks from the song's start
    dbtp: float

    @property
    def time_s(self) -> float:
        return self.index * STEP_S


def worst_blocks(
    peaks: Mapping[str, NDArray[np.float64]], count: int, separation_s: float
) -> list[Block]:
    """The ``count`` worst blocks across songs, worst first; within a song, blocks closer than
    ``separation_s`` to a worse one belong to the same transient and are skipped."""
    apart = round(separation_s / STEP_S)
    ranked = sorted(
        (Block(song, int(i), float(p[i])) for song, p in peaks.items() for i in range(len(p))),
        key=lambda b: -b.dbtp,
    )
    picked: list[Block] = []
    for block in ranked:
        if all(b.song != block.song or abs(b.index - block.index) >= apart for b in picked):
            picked.append(block)
            if len(picked) == count:
                break
    return picked
