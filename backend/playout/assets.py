"""Generated noise used by the session script: a quiet filler loop and the static intro."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NoiseSpec:
    """Pink noise: length in seconds and linear amplitude in (0, 1]."""

    duration_s: float
    amplitude: float

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError(f"NoiseSpec.duration_s must be > 0, got {self.duration_s}")
        if not 0 < self.amplitude <= 1:
            raise ValueError(f"NoiseSpec.amplitude must be in (0, 1], got {self.amplitude}")


# The filler's track boundaries drive backend retries, so it stays short; at 0.01 it sits
# near -57 dBFS RMS, under the gate's -30 dB gap threshold but never dead air.
FILLER = NoiseSpec(duration_s=2.0, amplitude=0.01)
STATIC_INTRO = NoiseSpec(duration_s=3.0, amplitude=0.3)


@dataclass(frozen=True)
class StreamAssets:
    filler: Path
    static_intro: Path


def _noise_command(ffmpeg: str, out: Path, spec: NoiseSpec) -> list[str]:
    """The ffmpeg command line that writes ``spec`` as stereo 44.1 kHz FLAC to ``out``."""
    source = (
        f"anoisesrc=color=pink:amplitude={spec.amplitude}:"
        f"duration={spec.duration_s}:sample_rate=44100"
    )
    return [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        source,
        "-ac",
        "2",
        "-c:a",
        "flac",
        str(out),
    ]


def _generate(ffmpeg: str, out: Path, spec: NoiseSpec) -> Path:
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        staged = out.with_suffix(".partial.flac")
        subprocess.run(_noise_command(ffmpeg, staged, spec), check=True, timeout=30)
        staged.replace(out)  # a crash mid-write never leaves a truncated asset behind
    return out


def ensure_stream_assets(ffmpeg: str, assets_dir: Path) -> StreamAssets:
    """Generate the filler and intro in ``assets_dir`` unless they already exist."""
    return StreamAssets(
        filler=_generate(ffmpeg, assets_dir / "filler.flac", FILLER),
        static_intro=_generate(ffmpeg, assets_dir / "static_intro.flac", STATIC_INTRO),
    )
