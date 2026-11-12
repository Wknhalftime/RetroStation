"""A song's loudness tail read with FFmpeg, for the ending rules (EOM ending rules, stage 1).

``read_tail`` decodes a file to mono 32-bit float at ``sample_rate`` (16000 Hz, so a 50 ms
frame is exactly 800 samples), takes the mean-square energy of every 50 ms frame, and gives
each frame in dB relative to the mean energy of the whole song's frames louder than
``floor_db``. It returns the frames from ``tail_ms`` before ``cue_out_ms`` (or the start) to the
end of the file. ``scripts/extract_ending_fixtures.py`` follows the same definition, and
``tests/playout/test_loudness.py`` pins it.

It imports ``LoudnessTail`` from the domain, the one exception to "playout takes primitives"
(see the import contract in ``pyproject.toml``).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import numpy as np

from backend.domain.endings import LoudnessTail
from backend.playout.liquidsoap_process import NO_WINDOW

_SILENT_ENERGY = 1e-12
"""The energy of a frame of digital silence, so its level is finite (-120 dB)."""


class LoudnessReadError(Exception):
    """This file's loudness could not be read: not audio, missing, or FFmpeg failed on it."""


class LoudnessToolError(Exception):
    """FFmpeg itself could not be started: the configured program is missing."""


@dataclass(frozen=True)
class LoudnessSettings:
    ffmpeg: str
    sample_rate: int = 16000
    frame_ms: int = 50
    tail_ms: int = 60_000
    floor_db: float = -60.0
    timeout_s: float = 60.0


def _decode_command(path: str, settings: LoudnessSettings) -> list[str]:
    """The ffmpeg command line that writes ``path`` as mono f32le PCM to stdout."""
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
        "f32le",
        "-",
    ]


def _last_line(stderr: bytes) -> str:
    lines = stderr.decode("utf-8", errors="replace").strip().splitlines()
    return lines[-1].strip() if lines else ""


def _decode(path: str, settings: LoudnessSettings) -> bytes:
    try:
        done = subprocess.run(
            _decode_command(path, settings),
            stdin=subprocess.DEVNULL,  # ffmpeg reads keys from an inherited console
            capture_output=True,
            timeout=settings.timeout_s,
            check=False,
            creationflags=NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise LoudnessToolError(f"ffmpeg not found ({settings.ffmpeg}): {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LoudnessReadError(f"ffmpeg timed out after {settings.timeout_s}s on {path}") from exc
    except OSError as exc:  # not executable, a directory
        raise LoudnessToolError(f"ffmpeg could not start ({settings.ffmpeg}): {exc}") from exc
    if done.returncode != 0:
        raise LoudnessReadError(_last_line(done.stderr) or f"ffmpeg exited {done.returncode}")
    if not done.stdout:
        raise LoudnessReadError(f"ffmpeg decoded no audio from {path}")
    return done.stdout


def _frame_levels_db(pcm: bytes, frame: int, floor_db: float) -> np.ndarray:
    """Every frame's level in dB relative to the mean energy of the frames above the floor."""
    samples = np.frombuffer(pcm[: len(pcm) // 4 * 4], dtype="<f4").astype(np.float64)
    count = -(-len(samples) // frame)
    padded = np.zeros(count * frame)
    padded[: len(samples)] = samples
    energy = np.maximum((padded.reshape(count, frame) ** 2).mean(axis=1), _SILENT_ENERGY)
    level = 10 * np.log10(energy)
    loud = energy[level > floor_db]
    reference = 10 * np.log10(loud.mean()) if len(loud) else 0.0
    return np.asarray(level - reference)


def read_tail(path: str, cue_out_ms: int, settings: LoudnessSettings) -> LoudnessTail:
    """``path``'s loudness from ``settings.tail_ms`` before ``cue_out_ms`` to the file's end.

    Raises ``LoudnessToolError`` when FFmpeg cannot be started, and ``LoudnessReadError`` for
    anything wrong with this one file.
    """
    frame = settings.sample_rate * settings.frame_ms // 1000
    levels = _frame_levels_db(_decode(path, settings), frame, settings.floor_db)
    start = min(max(0, (cue_out_ms - settings.tail_ms) // settings.frame_ms), len(levels) - 1)
    return LoudnessTail(
        start * settings.frame_ms,
        settings.frame_ms,
        tuple(float(v) for v in levels[start:]),
    )
