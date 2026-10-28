"""Codec round trip (P5): encode with the stream's settings, decode to float, measure again.

The Liquidsoap back end writes its MP3 itself, through the same encoder as the stream; renders
from the FFmpeg back end are encoded here with standalone FFmpeg's libmp3lame. Decoding always
uses standalone FFmpeg (float output, no clipping).

LAME scales its input before a CBR or ABR encode, by a factor from its preset table (presets.c,
abr_switch_map: 0.95 up to 160 kb/s, 0.97 at 192, 0.98 at 224, 1.0 from 256). At 128k, the
stream's bitrate until R20, every decoded level is therefore 0.45 dB under the input; at the
stream's 256k the level is kept. Measured 2026-10-24: -0.445 dB on tones from 100 Hz to 5 kHz
and -20 to -6 dBFS, through both FFmpeg builds (Liquidsoap's 7.1, the
stream's own encoder, and standalone 8.0.1) and both MP3 decoders, residual -97 dB (a pure gain);
-0.264 / -0.175 / 0.000 dB at 192 / 224 / 256k; none in VBR. FFmpeg exposes no option to turn
it off, so the stream has it, and decoded true peaks here include it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# The stream's encoder settings (output.harbor in backend/playout/session.liq; a test keeps the
# two the same). 256k since R20 (Lance, 2026-10-28), 128k before.
MP3_BITRATE = "256k"
MP3_RATE = 44100
LAME_SCALE_AT_128K = 0.95


def _ffmpeg(ffmpeg: Path, *args: str) -> None:
    run = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostats", "-y", *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if run.returncode != 0:
        raise RuntimeError(f"ffmpeg {' '.join(args)} failed:\n{run.stderr[-4000:]}")


def encode_mp3(ffmpeg: Path, wav: Path, mp3: Path, bitrate: str = MP3_BITRATE) -> Path:
    """CBR MP3 at the stream's rate, and by default its bitrate."""
    _ffmpeg(ffmpeg, "-i", str(wav), "-c:a", "libmp3lame", "-b:a", bitrate,
            "-ar", str(MP3_RATE), str(mp3))  # fmt: skip
    return mp3


def decode(ffmpeg: Path, mp3: Path, wav: Path) -> Path:
    """Decode to 32-bit float WAV, so overshoots above full scale survive."""
    _ffmpeg(ffmpeg, "-i", str(mp3), "-c:a", "pcm_f32le", str(wav))
    return wav
