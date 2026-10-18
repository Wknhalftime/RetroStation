"""Waveform peaks read with FFmpeg: one 0-255 byte per bucket of decoded samples.

``read_peaks`` decodes a file to mono s16le at ``sample_rate`` and keeps the largest absolute
sample of every ``sample_rate // per_second`` samples, scaled ``round(255 * max / 32768)``.
A partial last bucket counts. It takes a plain path and knows nothing of files or databases.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import numpy as np

from backend.playout.liquidsoap_process import NO_WINDOW

_FULL_SCALE = 32768
_BYTE_MAX = 255


class PeakReadError(Exception):
    """FFmpeg could not produce peaks for a file (missing program, bad file, timeout)."""


@dataclass(frozen=True)
class PeakSettings:
    ffmpeg: str
    per_second: int = 200
    sample_rate: int = 8000
    timeout_s: float = 60.0


@dataclass(frozen=True)
class Peaks:
    per_second: int
    data: bytes  # one 0-255 value per bucket


def _decode_command(path: str, settings: PeakSettings) -> list[str]:
    """The ffmpeg command line that writes ``path`` as mono s16le PCM to stdout."""
    return [
        settings.ffmpeg,
        "-v",
        "error",
        "-i",
        path,
        "-ac",
        "1",
        "-ar",
        str(settings.sample_rate),
        "-f",
        "s16le",
        "-",
    ]


def _last_line(stderr: bytes) -> str:
    lines = stderr.decode("utf-8", errors="replace").strip().splitlines()
    return lines[-1].strip() if lines else ""


def _decode(path: str, settings: PeakSettings) -> bytes:
    try:
        done = subprocess.run(
            _decode_command(path, settings),
            stdin=subprocess.DEVNULL,  # ffmpeg reads keys from an inherited console
            capture_output=True,
            timeout=settings.timeout_s,
            check=False,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise PeakReadError(f"ffmpeg timed out after {settings.timeout_s}s on {path}") from exc
    except OSError as exc:  # not found, not executable, a directory
        raise PeakReadError(f"ffmpeg could not start ({settings.ffmpeg}): {exc}") from exc
    if done.returncode != 0:
        reason = _last_line(done.stderr) or f"ffmpeg exited with {done.returncode}"
        raise PeakReadError(reason)
    return done.stdout


def _bucket_peaks(pcm: bytes, bucket: int) -> bytes:
    samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
    padded = -len(samples) % bucket
    if padded:
        samples = np.concatenate([samples, np.zeros(padded, dtype="<i2")])
    # Reduce in int16, then widen only the per-bucket extremes: abs(-32768) overflows int16.
    buckets = samples.reshape(-1, bucket)
    top = buckets.max(axis=1).astype(np.int32)
    bottom = buckets.min(axis=1).astype(np.int32)
    loudest = np.maximum(top, -bottom)
    scaled = np.minimum(np.rint(_BYTE_MAX * loudest / _FULL_SCALE), _BYTE_MAX)
    return bytes(scaled.astype(np.uint8))


def read_peaks(path: str, settings: PeakSettings) -> Peaks:
    """The waveform peaks of ``path``, ``settings.per_second`` of them for every second."""
    bucket = settings.sample_rate // settings.per_second
    return Peaks(settings.per_second, _bucket_peaks(_decode(path, settings), bucket))
