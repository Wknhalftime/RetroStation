"""The two render back ends, run for real: Liquidsoap (authoritative for final values) and
standalone FFmpeg. Each render reports its CPU time; the Liquidsoap one also writes the
stream's MP3 and the per-frame gauges the chain registers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, render

FS = 44100
FFMPEG = shutil.which("ffmpeg")
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")

needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
needs_liquidsoap = pytest.mark.skipif(
    LIQUIDSOAP is None or FFMPEG is None, reason="LIQUIDSOAP_PATH unset or ffmpeg missing"
)


def sine_file(path: Path, level_dbfs: float = -6.0, seconds: float = 1.0) -> Path:
    t = np.arange(int(seconds * FS)) / FS
    mono = 10 ** (level_dbfs / 20) * np.sin(2 * np.pi * 1000.0 * t)
    audio_io.write_wav(path, np.column_stack([mono, mono]), FS)
    return path


def peak_db(path: Path) -> float:
    x, _ = audio_io.read_wav(path)
    return float(20 * np.log10(np.max(np.abs(x))))


def test_fill_graph_puts_params_into_the_placeholders() -> None:
    graph = render.fill_graph("[0:a]volume=$gain[out]", {"gain": 0.5})

    assert graph == "[0:a]volume=0.5[out]"


def test_fill_graph_names_a_param_the_preset_lacks() -> None:
    with pytest.raises(render.RenderError, match="gain"):
        render.fill_graph("[0:a]volume=$gain[out]", {})


@pytest.mark.slow
@needs_ffmpeg
def test_ffmpeg_render_applies_the_graph_and_times_itself(tmp_path: Path) -> None:
    assert FFMPEG is not None
    source = sine_file(tmp_path / "in.wav")

    run = render.render_ffmpeg(Path(FFMPEG), "[0:a]volume=0.5[out]", source, tmp_path / "o.wav")

    assert peak_db(run.output) == pytest.approx(-12.02, abs=0.05)
    assert run.audio_seconds == pytest.approx(1.0)
    assert run.cpu_seconds >= 0.0
    assert run.mp3 is None and run.gauges is None


@needs_ffmpeg
def test_ffmpeg_version_names_the_build_and_its_libavfilter() -> None:
    assert FFMPEG is not None
    version = render.ffmpeg_version(Path(FFMPEG))

    assert version.startswith("ffmpeg ")
    assert "libavfilter" in version


@pytest.mark.slow
@pytest.mark.timeout(180)
@needs_liquidsoap
def test_liquidsoap_render_runs_the_chain_with_params_and_logs_gauges(tmp_path: Path) -> None:
    assert LIQUIDSOAP is not None
    chain = tmp_path / "chain.liq"
    chain.write_text(
        'def chain(s) =\n  g = param_float("gain", 1.)\n  gauge("gain", {g})\n'
        "  amplify(g, s)\nend\n",
        encoding="utf-8",
    )
    source = sine_file(tmp_path / "in.wav", seconds=2.0)

    run = render.render_liquidsoap(
        Path(LIQUIDSOAP), chain, {"gain": 0.5}, source, tmp_path / "out.wav", tmp_path / "cache"
    )

    assert peak_db(run.output) == pytest.approx(-12.02, abs=0.05)
    assert run.mp3 is not None and run.mp3.stat().st_size > 10_000
    assert run.gauges is not None
    rows = run.gauges.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "time_s,name,value"
    times = [float(r.split(",")[0]) for r in rows[1:]]
    assert len(times) >= 90  # 2 s at 20 ms frames
    assert times == sorted(times)
    assert {r.split(",")[1] for r in rows[1:]} == {"gain"}
    assert {float(r.split(",")[2]) for r in rows[1:]} == {0.5}


@pytest.mark.slow
@pytest.mark.timeout(180)
@needs_liquidsoap
def test_liquidsoap_errors_carry_what_liquidsoap_printed(tmp_path: Path) -> None:
    assert LIQUIDSOAP is not None
    chain = tmp_path / "chain.liq"
    chain.write_text("def chain(s) = no_such_operator(s) end\n", encoding="utf-8")

    with pytest.raises(render.RenderError, match="no_such_operator"):
        render.render_liquidsoap(
            Path(LIQUIDSOAP),
            chain,
            {},
            sine_file(tmp_path / "in.wav"),
            tmp_path / "o.wav",
            tmp_path / "cache",
        )


@needs_liquidsoap
def test_liquidsoap_version_includes_its_linked_ffmpeg_libraries() -> None:
    assert LIQUIDSOAP is not None
    version = render.liquidsoap_version(Path(LIQUIDSOAP))

    assert version.startswith("Liquidsoap 2.4.5")
    assert "libavcodec" in version and "libavformat" in version
