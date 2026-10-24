"""Codec round trip (P5): encode with the stream's settings, decode to float, measure again.

The Liquidsoap back end writes its MP3 itself, through the same encoder as the stream; renders
from the FFmpeg back end are encoded here with standalone FFmpeg's libmp3lame. Decoding always
uses standalone FFmpeg (float output, no clipping).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# The stream's encoder settings (output.harbor in backend/playout/session.liq).
MP3_BITRATE = "128k"
MP3_RATE = 44100


def _ffmpeg(ffmpeg: Path, *args: str) -> None:
    run = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostats", "-y", *args], capture_output=True, text=True
    )
    if run.returncode != 0:
        raise RuntimeError(f"ffmpeg {' '.join(args)} failed:\n{run.stderr[-4000:]}")


def encode_mp3(ffmpeg: Path, wav: Path, mp3: Path) -> Path:
    """MP3 at the stream's bitrate and rate."""
    _ffmpeg(ffmpeg, "-i", str(wav), "-c:a", "libmp3lame", "-b:a", MP3_BITRATE,
            "-ar", str(MP3_RATE), str(mp3))  # fmt: skip
    return mp3


def decode(ffmpeg: Path, mp3: Path, wav: Path) -> Path:
    """Decode to 32-bit float WAV, so overshoots above full scale survive."""
    _ffmpeg(ffmpeg, "-i", str(mp3), "-c:a", "pcm_f32le", str(wav))
    return wav
