"""Generated filler and intro noise (spec D12: the first intro is generated static)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.playout.assets import FILLER, STATIC_INTRO, NoiseSpec, ensure_stream_assets

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)


def _probe(path: Path) -> dict[str, object]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,channels,sample_rate:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    return {
        "codec": stream["codec_name"],
        "channels": stream["channels"],
        "sample_rate": int(stream["sample_rate"]),
        "duration": float(data["format"]["duration"]),
    }


def test_noise_spec_rejects_non_positive_duration() -> None:
    with pytest.raises(ValueError, match="NoiseSpec.duration_s"):
        NoiseSpec(0.0, 0.1)


def test_noise_spec_rejects_amplitude_out_of_range() -> None:
    with pytest.raises(ValueError, match="NoiseSpec.amplitude"):
        NoiseSpec(2.0, 1.5)


def test_builtin_specs_are_quiet_filler_and_louder_intro() -> None:
    assert FILLER.amplitude < STATIC_INTRO.amplitude


def test_filler_is_short_enough_to_retry_often() -> None:
    # Each filler track boundary retries the backend (spec: backend slow -> filler, resume).
    assert FILLER.duration_s <= 2.0


@pytest.mark.slow
@needs_ffmpeg
@pytest.mark.parametrize(("name", "spec"), [("filler", FILLER), ("static_intro", STATIC_INTRO)])
def test_generated_asset_is_stereo_44k1_flac_of_spec_length(
    tmp_path: Path, name: str, spec: NoiseSpec
) -> None:
    path: Path = getattr(ensure_stream_assets("ffmpeg", tmp_path), name)
    probed = _probe(path)
    assert probed["codec"] == "flac"
    assert probed["channels"] == 2
    assert probed["sample_rate"] == 44100
    assert probed["duration"] == pytest.approx(spec.duration_s, abs=0.05)


@pytest.mark.slow
@needs_ffmpeg
def test_ensure_stream_assets_generates_once(tmp_path: Path) -> None:
    assets = ensure_stream_assets("ffmpeg", tmp_path)
    first = (assets.filler.stat().st_mtime_ns, assets.static_intro.stat().st_mtime_ns)
    again = ensure_stream_assets("ffmpeg", tmp_path)
    assert again == assets
    assert (again.filler.stat().st_mtime_ns, again.static_intro.stat().st_mtime_ns) == first
